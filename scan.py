"""Rank swing-trade candidates for the configured small account."""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone

import config as config_mod
from broker import Account
from data import MarketData
from filters import FinnhubFilters, price_ok, spread_ok, window_ok
from risk import RiskManager
from strategy import TrendPullback


def scan(cfg) -> list[dict]:
    if not cfg.alpaca_key or not cfg.alpaca_secret:
        raise RuntimeError("Alpaca data credentials are required to scan current markets")
    cfg.mode = "paper"  # data-only; this command never constructs a broker or submits orders
    data = MarketData(cfg)
    strategy = TrendPullback(cfg)
    risk = RiskManager(cfg)
    filters = FinnhubFilters(cfg.finnhub_api_key)
    account = Account(cfg.account.starting_equity, cfg.account.starting_equity,
                      cfg.account.starting_equity)

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest
    client = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
    now = datetime.now(timezone.utc)
    market_window = window_ok(cfg, now)
    results = []
    for symbol in cfg.universe:
        row = {"symbol": symbol, "score": 0, "actionable": False, "reasons": []}
        if not market_window.ok:
            row["reasons"].append(f"market window: {market_window.reason}")
        try:
            q = client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
            bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
            last = (bid + ask) / 2 if bid and ask else bid or ask
            spread = (ask - bid) / last if last and bid and ask else 1.0
            row.update({"last": round(last, 2), "spread_pct": round(spread * 100, 3)})
            if not price_ok(last, cfg.strategy.minimum_price).ok:
                row["reasons"].append("below minimum price")
            # Outside the entry window, quote spreads are often stale or
            # one-sided. Report them, but don't misdiagnose them as liquidity.
            if market_window.ok and cfg.strategy.spread_filter and not spread_ok(spread).ok:
                row["reasons"].append("spread too wide")

            assessment = strategy.assess(
                symbol,
                data.get_bars(symbol, cfg.strategy.timeframe, 120),
                data.get_bars(symbol, cfg.strategy.confirmation_timeframe, 60),
            )
            row["score"] = assessment.score
            row["checks"] = assessment.checks
            row["reasons"].extend(assessment.reasons)

            earnings = filters.earnings_ok(symbol, now)
            news = filters.news_ok(symbol, now)
            if not earnings.ok:
                row["reasons"].append(earnings.reason)
            if not news.ok:
                row["reasons"].append(news.reason)
            warnings = [x.reason for x in (earnings, news) if x.ok and x.reason]
            if warnings:
                row["warnings"] = warnings

            if assessment.setup:
                setup = assessment.setup
                qty = risk.size(setup, account, "paper", 0)
                capital = qty * setup.entry
                risk_dollars = qty * (setup.entry - setup.stop)
                row.update({"entry": setup.entry, "stop": setup.stop,
                            "target": setup.target, "reward_risk": setup.reward_risk,
                            "shares": qty, "capital_required": round(capital, 2),
                            "risk_dollars": round(risk_dollars, 2),
                            "whole_shares": math.floor(qty),
                            "whole_share_capital": round(math.floor(qty) * setup.entry, 2),
                            "whole_share_risk": round(math.floor(qty) *
                                                      (setup.entry - setup.stop), 2)})
                row["actionable"] = not row["reasons"] and ask <= setup.entry
                if market_window.ok and ask > setup.entry:
                    row["reasons"].append(
                        f"current ask {ask:.2f} above limit entry {setup.entry:.2f}")
        except Exception as exc:  # one bad symbol must not abort the research run
            row["reasons"].append(f"data error: {type(exc).__name__}: {exc}")
        results.append(row)
    return sorted(results, key=lambda x: (x["actionable"], x["score"]), reverse=True)


def print_table(rows: list[dict]) -> None:
    print(f"{'SYM':<7} {'SCORE':>5} {'LAST':>9} {'ENTRY':>9} {'STOP':>9} "
          f"{'TARGET':>9} {'SHARES':>8} {'CAPITAL':>10}  STATUS")
    for row in rows:
        status = "ACTIONABLE" if row["actionable"] else "; ".join(row["reasons"][:2])
        def val(name):
            value = row.get(name)
            return "-" if value is None else str(value)
        print(f"{row['symbol']:<7} {row['score']:>5} {val('last'):>9} {val('entry'):>9} "
              f"{val('stop'):>9} {val('target'):>9} {val('shares'):>8} "
              f"{val('capital_required'):>10}  {status}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    cfg = config_mod.load()
    rows = scan(cfg)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print_table(rows)


if __name__ == "__main__":
    main()
