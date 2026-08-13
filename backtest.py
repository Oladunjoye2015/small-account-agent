"""Chronological backtest for one symbol using 15-minute OHLC CSV data.

CSV columns: timestamp, open, high, low, close. Timestamps must be ISO-8601.
Signals use completed bars only; entries occur on the following bar. When a
bar touches both stop and target, the stop is assumed first (conservative).
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import config as config_mod
from data import Bars
from strategy import TrendPullback

EASTERN = ZoneInfo("America/New_York")


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


def load_csv(path: str) -> list[Bar]:
    rows = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            rows.append(Bar(ts, *(float(row[k]) for k in ("open", "high", "low", "close"))))
    rows.sort(key=lambda x: x.ts)
    if any(b.high < max(b.open, b.close) or b.low > min(b.open, b.close) for b in rows):
        raise ValueError("invalid OHLC row")
    return rows


def aggregate_hourly(rows: list[Bar]) -> list[Bar]:
    out: list[Bar] = []
    bucket = []
    key = None
    for bar in rows:
        ts = bar.ts if bar.ts.tzinfo else bar.ts.replace(tzinfo=timezone.utc)
        et = ts.astimezone(EASTERN)
        session_open = datetime.combine(et.date(), time(9, 30), tzinfo=EASTERN)
        block = max(0, int((et - session_open).total_seconds() // 3600))
        new_key = session_open + timedelta(hours=block)
        if key is not None and new_key != key:
            out.append(Bar(key, bucket[0].open, max(x.high for x in bucket),
                           min(x.low for x in bucket), bucket[-1].close))
            bucket = []
        key = new_key
        bucket.append(bar)
    if bucket:
        out.append(Bar(key, bucket[0].open, max(x.high for x in bucket),
                       min(x.low for x in bucket), bucket[-1].close))
    return out


def _bars(rows: list[Bar]) -> Bars:
    return Bars([x.high for x in rows], [x.low for x in rows], [x.close for x in rows])


def run(rows: list[Bar], symbol: str, cfg, slippage_bps=2.0, fee=0.0) -> dict:
    if not rows:
        raise ValueError("backtest requires at least one bar")
    if cfg.strategy.regular_hours_only:
        rows = [bar for bar in rows
                if time(9, 30) <= (bar.ts if bar.ts.tzinfo else
                                    bar.ts.replace(tzinfo=timezone.utc)).astimezone(EASTERN).time()
                < time(16, 0)]
    if not rows:
        raise ValueError("no regular-session bars remain")
    hourly = aggregate_hourly(rows)
    strategy = TrendPullback(cfg)
    cash = float(cfg.account.starting_equity)
    position = None
    pending = None
    trades = []
    peak = cash
    max_dd = 0.0
    invested_bars = 0
    hour_idx = -1

    for i, bar in enumerate(rows):
        # Only expose fully completed hourly candles to the strategy.
        while (hour_idx + 1 < len(hourly)
               and hourly[hour_idx + 1].ts + timedelta(hours=1) <= bar.ts):
            hour_idx += 1

        if pending and position is None:
            setup = pending
            pending = None
            if bar.open <= setup.entry or bar.low <= setup.entry:
                raw = min(bar.open, setup.entry) if bar.open <= setup.entry else setup.entry
                entry = raw * (1 + slippage_bps / 10_000)
                risk_share = entry - setup.stop
                risk_cash = min(cash * cfg.risk.risk_per_trade_frac,
                                cfg.deployment.normal_trade_risk_dollars)
                qty = min(risk_cash / risk_share,
                          cash * cfg.risk.max_position_frac / entry,
                          cash / entry) if risk_share > 0 else 0
                if qty * entry >= 1:
                    cash -= qty * entry + fee
                    position = {"entry": entry, "stop": setup.stop,
                                "target": setup.target, "qty": qty, "opened": bar.ts}

        if position:
            invested_bars += 1
            exit_px = outcome = None
            if bar.low <= position["stop"]:
                exit_px = min(bar.open, position["stop"]) * (1 - slippage_bps / 10_000)
                outcome = "stop"
            elif bar.high >= position["target"]:
                exit_px = max(bar.open, position["target"]) * (1 - slippage_bps / 10_000)
                outcome = "target"
            if exit_px is not None:
                proceeds = position["qty"] * exit_px - fee
                pnl = proceeds - position["qty"] * position["entry"] - fee
                cash += proceeds
                trades.append({"opened": position["opened"].isoformat(), "closed": bar.ts.isoformat(),
                               "entry": round(position["entry"], 4), "exit": round(exit_px, 4),
                               "stop": round(position["stop"], 4),
                               "target": round(position["target"], 4),
                               "qty": round(position["qty"], 4), "pnl": round(pnl, 2),
                               "outcome": outcome})
                position = None

        equity = cash + (position["qty"] * bar.close if position else 0)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0)

        if position is None and pending is None and hour_idx >= 56 and i >= 60:
            setup = strategy.generate(symbol, _bars(hourly[max(0, hour_idx - 119):hour_idx + 1]),
                                      _bars(rows[i - 59:i + 1]))
            if setup:
                pending = setup

    final_equity = cash + (position["qty"] * rows[-1].close if position and rows else 0)
    wins = sum(t["pnl"] > 0 for t in trades)
    gross_win = sum(max(0, t["pnl"]) for t in trades)
    gross_loss = -sum(min(0, t["pnl"]) for t in trades)
    net = final_equity - cfg.account.starting_equity
    buy_hold = (rows[-1].close / rows[0].open - 1) * 100
    return {"symbol": symbol, "starting_equity": cfg.account.starting_equity,
            "final_equity": round(final_equity, 2),
            "return_pct": round((final_equity / cfg.account.starting_equity - 1) * 100, 2),
            "buy_hold_return_pct": round(buy_hold, 2),
            "excess_vs_buy_hold_pct": round((final_equity / cfg.account.starting_equity - 1) * 100
                                             - buy_hold, 2),
            "max_drawdown_pct": round(max_dd * 100, 2), "trades": len(trades),
            "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0,
            "expectancy_dollars": round(net / len(trades), 2) if trades else 0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "exposure_pct": round(invested_bars / len(rows) * 100, 2),
            "open_position": bool(position), "trade_log": trades}


def yearly_reports(rows: list[Bar], symbol: str, cfg, slippage_bps=2.0, fee=0.0) -> list[dict]:
    """Independent calendar-year runs expose regime dependence."""
    years = sorted({bar.ts.year for bar in rows})
    reports = []
    for year in years:
        subset = [bar for bar in rows if bar.ts.year == year]
        try:
            report = run(subset, symbol, cfg, slippage_bps, fee)
        except ValueError as exc:
            if "no regular-session bars remain" in str(exc):
                continue
            raise
        report.pop("trade_log", None)
        report["year"] = year
        reports.append(report)
    return reports


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--fee", type=float, default=0.0)
    parser.add_argument("--yearly", action="store_true",
                        help="include independent calendar-year regime reports")
    args = parser.parse_args()
    report = run(load_csv(args.csv), args.symbol.upper(), config_mod.load(),
                 args.slippage_bps, args.fee)
    if args.yearly:
        report["yearly"] = yearly_reports(load_csv(args.csv), args.symbol.upper(),
                                          config_mod.load(), args.slippage_bps, args.fee)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
