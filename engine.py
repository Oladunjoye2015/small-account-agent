"""The engine: one cycle wires everything together.

Order of a cycle:
  1. Mark equity / update the drawdown peak.
  2. Execution hygiene (Alpaca): cancel stale unfilled entries; if a held
     position has lost its protective stop, flatten it.
  3. Manage brackets (sim): close positions that hit stop/target, record them.
  4. Portfolio gate: if a limit is breached, don't open anything (and hard-halt
     on a drawdown breach).
  5. If allowed and inside the trading window, scan the universe for a
     trend-pullback setup that passes every filter, size it, and place a bracket.

Only ONE new position is opened per cycle (max_open_positions is 1 by default),
and positions are held overnight — no same-day round-trips — so the account
never accrues Pattern-Day-Trader flags.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config as config_mod
from broker import AlpacaBroker, SimBroker, VirtualBroker
from data import MarketData
from filters import (FinnhubFilters, price_ok, spread_ok, window_ok)
from risk import RiskManager
from state import State
from strategy import TrendPullback


def build_broker(cfg, state):
    if cfg.mode == "sim":
        return SimBroker(cfg.universe, starting_cash=cfg.account.starting_equity)
    if cfg.mode in ("paper", "live"):
        # Virtual $2k account on REAL Alpaca market data. This deliberately does
        # NOT place orders on the oversized/shared Alpaca account — it simulates
        # the small account internally so sizing, P&L and drawdown are honest.
        # (True real-money live would need a correctly-sized account + AlpacaBroker.)
        return VirtualBroker(cfg, state)
    raise ValueError(f"unknown mode {cfg.mode}")


class SwingEngine:
    def __init__(self, cfg, state: State | None = None):
        self.cfg = cfg
        self.state = state or State()
        self.broker = build_broker(cfg, self.state)
        self.data = MarketData(cfg)
        self.strategy = TrendPullback(cfg)
        self.risk = RiskManager(cfg)
        self.finnhub = FinnhubFilters(cfg.finnhub_api_key)
        self.state.log(f"engine started | mode={cfg.mode} | universe={','.join(cfg.universe)} "
                       f"| equity={cfg.account.starting_equity}")

    def run_cycle(self, now=None) -> dict:
        now = now or datetime.now(timezone.utc)
        events: list[str] = []
        account = self.broker.get_account()
        self.state.peak_equity(account.equity)
        self.state.record_equity(round(account.equity, 2), round(account.cash, 2))

        # 2. Execution hygiene (real broker only).
        if isinstance(self.broker, AlpacaBroker):
            try:
                n = self.broker.cancel_stale_entries(self.cfg.execution.cancel_stale_entry_minutes)
                if n:
                    events.append(f"cancelled {n} stale entries")
                if self.cfg.execution.close_if_stop_missing:
                    for sym in self.broker.positions_missing_stop():
                        self.broker.close_position(sym)
                        self.state.log(f"closed {sym}: protective stop missing", level="warn")
                        events.append(f"stop-missing close {sym}")
            except Exception as exc:  # noqa: BLE001
                self.state.log(f"hygiene error: {exc!r}", level="error")

        # 3. Manage brackets (sim + virtual): close on stop/target, record.
        if hasattr(self.broker, "manage"):
            for f in self.broker.manage():
                self.state.record_trade(f.symbol, "sell", f.qty, None, f.price,
                                        f.realized_pl, f.outcome, self.cfg.mode)
                self.state.log(f"EXIT {f.symbol} {f.outcome} @ {f.price} pl={f.realized_pl:+.2f}")
                events.append(f"exit {f.symbol} ({f.outcome})")

        positions = self.broker.get_positions()

        # 4. Portfolio gate.
        gate = self.risk.portfolio_gate(account, positions, self.state, now)
        if not gate.can_open:
            self.state.log(f"cycle: no entries — {gate.reason}"
                           + (" [HARD HALT]" if gate.hard_halt else ""))
            return self._status(account, positions, events, gate.reason)

        # Trading-window check (real market hours; not applicable to the sim).
        if self.cfg.mode != "sim":
            w = window_ok(self.cfg, now)
            if not w.ok:
                return self._status(account, positions, events, f"window: {w.reason}")

        # 5. Scan universe for one setup.
        held = {p.symbol for p in positions}
        live_done = self.state.total_trades(mode="live")
        for sym in self.cfg.universe:
            if sym in held or self.broker.has_open_entry(sym):
                continue
            try:
                quote = self.broker.get_quote(sym)
            except Exception:
                continue

            if not price_ok(quote.last, self.cfg.strategy.minimum_price).ok:
                continue
            if self.cfg.strategy.spread_filter and not spread_ok(quote.spread_pct).ok:
                continue
            if self.cfg.strategy.earnings_filter:
                er = self.finnhub.earnings_ok(sym, now)
                if not er.ok:
                    self.state.log(f"skip {sym}: {er.reason}")
                    continue
            if self.cfg.strategy.news_filter:
                nr = self.finnhub.news_ok(sym, now)
                if not nr.ok:
                    self.state.log(f"skip {sym}: {nr.reason}")
                    continue

            bars_1h = self.data.get_bars(sym, self.cfg.strategy.timeframe, 120)
            bars_15 = self.data.get_bars(sym, self.cfg.strategy.confirmation_timeframe, 60)
            setup = self.strategy.generate(sym, bars_1h, bars_15)
            if setup is None:
                continue
            if setup.reward_risk < self.cfg.risk.minimum_reward_risk:
                continue

            shares = self.risk.size(setup, account, self.cfg.mode, live_done)
            if shares < 1:
                self.state.log(f"skip {sym}: size < 1 share for ${setup.entry}")
                continue

            oid = self.broker.submit_bracket(sym, shares, setup.entry, setup.stop, setup.target)
            if oid:
                self.state.record_trade(sym, "buy", shares, setup.entry, None, 0.0,
                                        setup.reason, self.cfg.mode)
                self.state.log(f"ENTER {sym} x{shares} @ {setup.entry} "
                               f"stop {setup.stop} tgt {setup.target} rr {setup.reward_risk}")
                events.append(f"enter {sym} x{shares}")
                break  # one position at a time

        return self._status(account, positions, events, "ok")

    def status(self) -> dict:
        """Read-only snapshot for the API/dashboard (does not run a cycle)."""
        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
        except Exception as exc:  # noqa: BLE001
            return {"mode": self.cfg.mode, "error": repr(exc)}
        peak = float(self.state.get_meta("peak_equity", account.equity) or account.equity)
        dd = max(0.0, (peak - account.equity) / peak) if peak else 0.0
        s = self._status(account, positions, [], "ok")
        s.update({
            "backend": getattr(self.state, "backend", "sqlite"),
            "starting_equity": self.cfg.account.starting_equity,
            "pnl": round(account.equity - self.cfg.account.starting_equity, 2),
            "peak_equity": round(peak, 2),
            "drawdown_pct": round(dd * 100, 2),
            "paper_trades_done": self.state.total_trades(mode="paper"),
            "paper_minimum_trades": self.cfg.deployment.paper_minimum_trades,
            "open_positions_detail": [
                {"symbol": p.symbol, "qty": p.qty, "avg_entry_price": p.avg_entry_price,
                 "stop": p.stop, "target": p.target, "unrealized_pl": p.unrealized_pl}
                for p in positions
            ],
        })
        return s

    def _status(self, account, positions, events, note):
        return {
            "mode": self.cfg.mode,
            "equity": round(account.equity, 2),
            "cash": round(account.cash, 2),
            "open_positions": [p.symbol for p in positions],
            "trades_today": self.state.trades_today(),
            "total_trades": self.state.total_trades(),
            "pl_today": round(self.state.pl_today(), 2),
            "pl_week": round(self.state.pl_this_week(), 2),
            "consecutive_losses": self.state.consecutive_losses(),
            "note": note,
            "events": events,
        }
