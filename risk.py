"""Risk manager — the layer that keeps a small account alive.

Two jobs:
  * ``portfolio_gate`` — decide whether ANY new trade is allowed right now,
    enforcing the account-level limits: max drawdown (hard halt), daily and
    weekly loss limits, a consecutive-loss pause, the per-day trade cap, and the
    max-open-positions cap.
  * ``size`` — turn a setup into a whole-share quantity so that hitting the stop
    costs about ``risk_per_trade`` of equity, capped by the position-size limit,
    available cash (no margin), and the deployment dollar caps.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Gate:
    can_open: bool
    hard_halt: bool          # drawdown breach -> stop everything
    reason: str = ""


class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.r = cfg.risk

    def portfolio_gate(self, account, positions, state, now=None) -> Gate:
        equity = account.equity
        peak = state.peak_equity(equity)

        # Hard halt: max drawdown from peak equity.
        if peak > 0 and (peak - equity) / peak >= self.r.max_drawdown_frac:
            return Gate(False, True, f"max drawdown {(peak-equity)/peak*100:.1f}% >= "
                                     f"{self.r.max_drawdown_pct}%")

        # Daily / weekly realized-loss limits (as % of current equity).
        if state.pl_today(now) <= -equity * self.r.daily_loss_limit_frac:
            return Gate(False, False, f"daily loss limit ({self.r.daily_loss_limit_pct}%) hit")
        if state.pl_this_week(now) <= -equity * self.r.weekly_loss_limit_frac:
            return Gate(False, False, f"weekly loss limit ({self.r.weekly_loss_limit_pct}%) hit")

        # Consecutive-loss pause.
        if state.consecutive_losses() >= self.r.consecutive_loss_limit:
            return Gate(False, False, f"{self.r.consecutive_loss_limit} losses in a row -- paused")

        # Per-day trade cap and max open positions.
        if state.trades_today(now) >= self.r.max_trades_per_day:
            return Gate(False, False, f"max {self.r.max_trades_per_day} trades/day reached")
        if len(positions) >= self.r.max_open_positions:
            return Gate(False, False, "max open positions reached")

        return Gate(True, False, "ok")

    def size(self, setup, account, mode: str, live_trades_done: int) -> float:
        """Fractional-share position sizing. Whole shares make a $2k account
        untradeable on $200+ stocks (one share blows the risk budget); the
        virtual/sim ledger supports fractions, so we size by dollars instead."""
        risk_per_share = setup.entry - setup.stop
        if risk_per_share <= 0:
            return 0.0
        # Dollars to risk: percentage of equity, capped by the deployment dollar
        # limits (and a smaller cap for the very first live trades).
        risk_dollars = account.equity * self.r.risk_per_trade_frac
        d = self.cfg.deployment
        risk_dollars = min(risk_dollars, d.normal_trade_risk_dollars)
        if mode == "live" and live_trades_done < 5:
            risk_dollars = min(risk_dollars, d.first_live_trade_risk_dollars)

        shares = risk_dollars / risk_per_share
        # Cap by max position size and available cash (cash account, no margin).
        shares = min(shares, account.equity * self.r.max_position_frac / setup.entry)
        shares = min(shares, account.cash / setup.entry)
        return round(max(0.0, shares), 4)
