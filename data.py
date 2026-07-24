"""Market data: multi-timeframe OHLC bars for the strategy.

Provides ``get_bars(symbol, timeframe, n)`` returning recent highs/lows/closes.
In paper/live it pulls real bars from Alpaca; in sim it generates a persistent
synthetic series per (symbol, timeframe) so the full pipeline runs offline.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class Bars:
    highs: list
    lows: list
    closes: list

    def __len__(self) -> int:
        return len(self.closes)


_TF_MINUTES = {"1Min": 1, "5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60, "1Day": 390}


class MarketData:
    def __init__(self, cfg):
        self.cfg = cfg
        self._sim: dict[tuple, list] = {}          # (sym, tf) -> [(h,l,c), ...]
        self._client = None
        if cfg.mode in ("paper", "live"):
            from alpaca.data.historical import StockHistoricalDataClient
            self._client = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)

    def get_bars(self, symbol: str, timeframe: str, n: int = 120) -> Bars:
        if self.cfg.mode == "sim":
            return self._sim_bars(symbol, timeframe, n)
        return self._alpaca_bars(symbol, timeframe, n)

    # ---- sim ------------------------------------------------------------- #
    def _sim_bars(self, symbol, timeframe, n) -> Bars:
        key = (symbol, timeframe)
        series = self._sim.get(key)
        if series is None:
            rng = random.Random(hash(key) & 0xFFFF)
            price = rng.uniform(30, 120)
            drift = rng.uniform(-0.0002, 0.0006)   # some symbols trend up
            series = []
            for _ in range(max(n, 120)):
                price = max(1.0, price * (1 + rng.gauss(drift, 0.008)))
                hi = price * (1 + abs(rng.gauss(0, 0.003)))
                lo = price * (1 - abs(rng.gauss(0, 0.003)))
                series.append((round(hi, 2), round(lo, 2), round(price, 2)))
            self._sim[key] = series
        else:
            rng = random.Random()
            last = series[-1][2]
            price = max(1.0, last * (1 + rng.gauss(0.0002, 0.008)))
            hi = price * (1 + abs(rng.gauss(0, 0.003)))
            lo = price * (1 - abs(rng.gauss(0, 0.003)))
            series.append((round(hi, 2), round(lo, 2), round(price, 2)))
            del series[:-max(n, 120)]
        tail = series[-n:]
        return Bars([b[0] for b in tail], [b[1] for b in tail], [b[2] for b in tail])

    # ---- alpaca ---------------------------------------------------------- #
    def _alpaca_bars(self, symbol, timeframe, n) -> Bars:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        tf_map = {
            "1Min": TimeFrame(1, TimeFrameUnit.Minute),
            "5Min": TimeFrame(5, TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "30Min": TimeFrame(30, TimeFrameUnit.Minute),
            "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
            "1Day": TimeFrame(1, TimeFrameUnit.Day),
        }
        minutes = _TF_MINUTES.get(timeframe, 60)
        # Pull enough calendar days to cover n bars of this timeframe (+ slack).
        days = max(5, int((n * minutes) / 390 * 1.6) + 3)
        end = datetime.now(timezone.utc) - timedelta(minutes=20)
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=tf_map[timeframe],
            start=end - timedelta(days=days), end=end, feed="iex",
        )
        data = getattr(self._client.get_stock_bars(req), "data", {}) or {}
        rows = data.get(symbol, [])
        rows = rows[-n:]
        return Bars([float(b.high) for b in rows],
                    [float(b.low) for b in rows],
                    [float(b.close) for b in rows])
