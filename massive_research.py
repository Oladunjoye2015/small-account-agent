"""Run this project's swing strategy against the shared Massive equity archive."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pandas as pd

import config as config_mod
from backtest import Bar, run, yearly_reports

DEFAULT_ROOT = Path("/Users/jodi-annhenry/tv-ml-trading-bot/data/massive_research")


def available_symbols(root: Path) -> list[str]:
    base = root / "processed" / "minute"
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def load_massive(root: Path, symbol: str, start_year=None, end_year=None) -> list[Bar]:
    folder = root / "processed" / "minute" / symbol.upper()
    if not folder.is_dir():
        raise FileNotFoundError(f"no Massive minute data for {symbol}")
    files = sorted(folder.glob("*.parquet"))
    if start_year is not None:
        files = [p for p in files if int(p.stem) >= start_year]
    if end_year is not None:
        files = [p for p in files if int(p.stem) <= end_year]
    if not files:
        raise FileNotFoundError(f"no selected years for {symbol}")

    frames = [pd.read_parquet(p, columns=["timestamp", "open", "high", "low", "close"])
              for p in files]
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.dropna().drop_duplicates("timestamp").sort_values("timestamp")
    df = df.set_index("timestamp")

    # Aggregate source minute bars to the strategy's 15-minute confirmation
    # timeframe. origin=start_day aligns US regular bars at :00/:15/:30/:45.
    bars = df.resample("15min", origin="start_day", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    return [Bar(ts.to_pydatetime(), float(r.open), float(r.high),
                float(r.low), float(r.close)) for ts, r in bars.iterrows()]


def research(root: Path, symbols: list[str], cfg, start_year=None, end_year=None,
             slippage_bps=2.0, fee=0.0) -> dict:
    reports = []
    errors = []
    for symbol in symbols:
        try:
            rows = load_massive(root, symbol, start_year, end_year)
            report = run(rows, symbol, cfg, slippage_bps, fee)
            report["yearly"] = yearly_reports(rows, symbol, cfg, slippage_bps, fee)
            report["bars_15m"] = len(rows)
            evaluable = [y for y in report["yearly"] if y["trades"] >= 10]
            report["profitable_evaluable_years"] = sum(y["return_pct"] > 0 for y in evaluable)
            report["evaluable_years"] = len(evaluable)
            report["robust_all_evaluable_years"] = bool(evaluable) and all(
                y["return_pct"] > 0 and (y["profit_factor"] or 0) > 1 for y in evaluable)
            closes = [bar.close for bar in rows]
            max_jump = max((abs(b / a - 1) for a, b in zip(closes, closes[1:]) if a), default=0)
            report["max_adjacent_bar_jump_pct"] = round(max_jump * 100, 2)
            if max_jump > 0.25:
                report["data_quality_warning"] = (
                    "adjacent price jump above 25%; verify ticker continuity/corporate actions"
                )
            report.pop("trade_log", None)
            reports.append(report)
        except Exception as exc:
            errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
    reports.sort(key=lambda x: (x["expectancy_dollars"], x["return_pct"]), reverse=True)
    viable = [x["symbol"] for x in reports
              if x["trades"] >= 30 and (x["profit_factor"] or 0) > 1.1
              and x["expectancy_dollars"] > 0]
    robust = [x["symbol"] for x in reports if x["robust_all_evaluable_years"]
              and x["evaluable_years"] >= 3 and "data_quality_warning" not in x]
    return {"dataset": str(root), "account_size": cfg.account.starting_equity,
            "start_year": start_year, "end_year": end_year,
            "symbols_requested": symbols, "viability_rule":
            "at least 30 trades, profit factor > 1.10, positive expectancy",
            "symbols_passing_initial_screen": viable,
            "symbols_robust_across_evaluable_years": robust,
            "results": reports, "errors": errors}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.getenv("MASSIVE_RESEARCH_ROOT", str(DEFAULT_ROOT)))
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--all-symbols", action="store_true")
    parser.add_argument("--start-year", type=int)
    parser.add_argument("--end-year", type=int)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--fee", type=float, default=0.0)
    parser.add_argument("--output")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    cfg = config_mod.load()
    present = available_symbols(root)
    symbols = present if args.all_symbols else (args.symbols or [s for s in cfg.universe if s in present])
    report = research(root, [s.upper() for s in symbols], cfg, args.start_year,
                      args.end_year, args.slippage_bps, args.fee)
    payload = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n")
        print(f"wrote {args.output}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
