"""Download adjusted 15-minute Alpaca stock bars to backtest-ready CSV."""
from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime, timezone


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def download(symbol: str, start: datetime, end: datetime, output: str,
             api_key: str, secret: str, feed: str = "iex") -> int:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    feeds = {"iex": DataFeed.IEX, "sip": DataFeed.SIP}
    client = StockHistoricalDataClient(api_key, secret)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=start,
        end=end,
        adjustment=Adjustment.ALL,
        feed=feeds[feed],
    )
    result = client.get_stock_bars(request)
    bars = (getattr(result, "data", {}) or {}).get(symbol, [])
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with open(output, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(("timestamp", "open", "high", "low", "close", "volume"))
        for bar in bars:
            writer.writerow((bar.timestamp.isoformat(), bar.open, bar.high,
                             bar.low, bar.close, bar.volume))
    return len(bars)


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (exclusive)")
    parser.add_argument("--output", required=True)
    parser.add_argument("--feed", choices=("iex", "sip"), default="iex")
    args = parser.parse_args()
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        raise SystemExit("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
    count = download(args.symbol.upper(), _date(args.start), _date(args.end),
                     args.output, key, secret, args.feed)
    print(f"wrote {count} bars to {args.output}")


if __name__ == "__main__":
    main()
