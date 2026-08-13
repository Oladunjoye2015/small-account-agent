"""Select and portfolio-test a few symbols for the virtual $2,000 account."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import config as config_mod
from backtest import run
from massive_research import DEFAULT_ROOT, available_symbols, load_massive


def _years(rows, first, last):
    return [bar for bar in rows if first <= bar.ts.year <= last]


def portfolio_overlay(reports: list[dict], cfg) -> dict:
    """Apply independent signals to one cash ledger with one open position."""
    rank = {r["symbol"]: i for i, r in enumerate(reports)}
    events = []
    for report in reports:
        for trade in report["holdout"]["trade_log"]:
            events.append((datetime.fromisoformat(trade["opened"]), rank[report["symbol"]],
                           report["symbol"], trade))
    events.sort()
    cash = float(cfg.account.starting_equity)
    closed_at = None
    accepted = []
    skipped_overlap = 0
    peak = cash
    max_dd = 0.0
    for opened, _, symbol, trade in events:
        if closed_at is not None and opened < closed_at:
            skipped_overlap += 1
            continue
        entry, stop, exit_price = trade["entry"], trade["stop"], trade["exit"]
        risk_share = entry - stop
        if risk_share <= 0:
            continue
        qty = min(cfg.deployment.normal_trade_risk_dollars / risk_share,
                  cash * cfg.risk.max_position_frac / entry, cash / entry)
        if qty * entry < 1:
            continue
        pnl = qty * (exit_price - entry)
        cash += pnl
        closed_at = datetime.fromisoformat(trade["closed"])
        accepted.append({"symbol": symbol, "opened": trade["opened"],
                         "closed": trade["closed"], "qty": round(qty, 4),
                         "entry": entry, "exit": exit_price, "pnl": round(pnl, 2),
                         "outcome": trade["outcome"]})
        peak = max(peak, cash)
        max_dd = max(max_dd, (peak - cash) / peak)
    wins = sum(t["pnl"] > 0 for t in accepted)
    gross_wins = sum(max(0, t["pnl"]) for t in accepted)
    gross_losses = -sum(min(0, t["pnl"]) for t in accepted)
    return {"starting_equity": cfg.account.starting_equity,
            "final_equity": round(cash, 2),
            "return_pct": round((cash / cfg.account.starting_equity - 1) * 100, 2),
            "max_drawdown_pct": round(max_dd * 100, 2), "trades": len(accepted),
            "win_rate_pct": round(wins / len(accepted) * 100, 2) if accepted else 0,
            "profit_factor": round(gross_wins / gross_losses, 2) if gross_losses else None,
            "skipped_overlapping_signals": skipped_overlap, "trade_log": accepted}


def select(root: Path, symbols: list[str], cfg, train_start=2021, train_end=2024,
           holdout_year=2025, limit=3) -> dict:
    evaluated = []
    for symbol in symbols:
        rows = load_massive(root, symbol, train_start, holdout_year)
        training = run(_years(rows, train_start, train_end), symbol, cfg)
        holdout = run(_years(rows, holdout_year, holdout_year), symbol, cfg)
        passed = (training["trades"] >= 30 and (training["profit_factor"] or 0) > 1.10
                  and training["expectancy_dollars"] > 0 and holdout["trades"] >= 10
                  and (holdout["profit_factor"] or 0) > 1.10
                  and holdout["expectancy_dollars"] > 0)
        evaluated.append({"symbol": symbol, "passed": passed, "training": training,
                          "holdout": holdout})
    evaluated.sort(key=lambda x: (x["passed"], x["holdout"]["profit_factor"] or 0,
                                  x["holdout"]["expectancy_dollars"]), reverse=True)
    finalists = [x for x in evaluated if x["passed"]][:limit]
    overlay = portfolio_overlay(finalists, cfg)
    return {"method": "2021-2024 selection; frozen 2025 holdout; shared-account overlay",
            "criteria": "training >=30 trades, training and holdout PF>1.10 and positive expectancy, holdout >=10 trades",
            "selected_symbols": [x["symbol"] for x in finalists],
            "portfolio_holdout": overlay, "evaluated": evaluated}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output", default="candidate-selection-report.json")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    cfg = config_mod.load()
    present = available_symbols(root)
    symbols = [s.upper() for s in (args.symbols or cfg.universe) if s.upper() in present]
    report = select(root, symbols, cfg, limit=args.limit)
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"selected_symbols": report["selected_symbols"],
                      "portfolio_holdout": {k: v for k, v in report["portfolio_holdout"].items()
                                             if k != "trade_log"}}, indent=2))


if __name__ == "__main__":
    main()
