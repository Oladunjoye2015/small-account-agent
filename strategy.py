"""Trend-pullback strategy (long only).

The idea, in plain terms: only buy strong stocks that are *already* in an
uptrend, but wait to buy them on a **pullback** rather than chasing — then
require the lower timeframe to confirm the bounce before entering. This is a
classic, robust swing setup, and unlike a scalper it holds for days, which is
exactly what keeps a small account clear of the Pattern Day Trader rule.

  1. TREND (1H):   fast EMA > slow EMA, price above the slow EMA, slow EMA rising.
  2. PULLBACK (1H): price recently dipped to the fast EMA zone and is holding
                    above it (buying the dip, not the breakout).
  3. CONFIRM (15m): short-term momentum has turned back up.

It returns a Setup with a concrete entry, a stop below the pullback low, and a
target set for at least the configured reward:risk. The risk manager has final
say on size and whether the trade is allowed at all.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Setup:
    symbol: str
    entry: float
    stop: float
    target: float
    reward_risk: float
    reason: str


def _ema(values, span: int) -> np.ndarray:
    a = np.asarray(values, dtype=float)
    if a.size == 0:
        return a
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(a)
    out[0] = a[0]
    for i in range(1, a.size):
        out[i] = alpha * a[i] + (1 - alpha) * out[i - 1]
    return out


class TrendPullback:
    def __init__(self, cfg):
        self.cfg = cfg
        self.fast, self.slow = 20, 50
        self.pullback_lookback = 6      # 1H bars to look for the dip
        self.pullback_band = 0.012      # dip within 1.2% of the fast EMA
        self.stop_buffer = 0.003        # stop this far below the pullback low
        self.min_rr = cfg.risk.minimum_reward_risk

    def generate(self, symbol: str, bars_1h, bars_15m) -> Setup | None:
        closes = bars_1h.closes
        lows = bars_1h.lows
        if len(closes) < self.slow + self.pullback_lookback + 2:
            return None
        if len(bars_15m.closes) < 30:
            return None

        ema_fast = _ema(closes, self.fast)
        ema_slow = _ema(closes, self.slow)
        price = closes[-1]

        # 1. Uptrend.
        uptrend = (ema_fast[-1] > ema_slow[-1]
                   and price > ema_slow[-1]
                   and ema_slow[-1] > ema_slow[-5])
        if not uptrend:
            return None

        # 2. Pullback: a recent low touched the fast-EMA zone, price now holding
        #    above the fast EMA (bounced).
        recent_low = min(lows[-self.pullback_lookback:])
        touched = recent_low <= ema_fast[-1] * (1 + self.pullback_band)
        holding = price > ema_fast[-1]
        if not (touched and holding):
            return None

        # 3. 15m confirmation: fast>slow EMA and momentum turning up.
        c15 = bars_15m.closes
        ef15, es15 = _ema(c15, 9), _ema(c15, 21)
        confirm = ef15[-1] > es15[-1] and c15[-1] > c15[-2] and c15[-1] > ef15[-1]
        if not confirm:
            return None

        # Entry / stop / target.
        entry = price
        stop = recent_low * (1 - self.stop_buffer)
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + max(self.min_rr, self.min_rr) * risk
        rr = (target - entry) / risk
        if rr < self.min_rr:
            return None

        return Setup(symbol=symbol, entry=round(entry, 2), stop=round(stop, 2),
                     target=round(target, 2), reward_risk=round(rr, 2),
                     reason="trend_pullback")
