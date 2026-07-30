"""Pre-trade filters: price, spread, trading-window, earnings, news.

Each filter answers one question: "is it OK to open a new position in this
symbol right now?" All external-data filters (earnings, news via Finnhub)
FAIL OPEN — if the data source is unreachable they allow the trade and say so,
rather than freezing the agent. Time/price/spread are local and deterministic.
"""
from __future__ import annotations

import json
import time as _time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Only genuinely high-impact events. Generic words like "lawsuit", "sued",
# "investigation", "downgrade" fire constantly on mega-caps and would block
# them nearly every day, so they're intentionally excluded.
NEWS_KEYWORDS = ("trading halt", "halted", "fraud", "bankruptcy", "recall",
                 "delisting", "subpoena", "cuts guidance", "profit warning",
                 "fda rejects", "restatement")


@dataclass
class FilterResult:
    ok: bool
    reason: str = ""


def _et_now(now=None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now - timedelta(hours=4)   # EDT approximation


# --- local filters --------------------------------------------------------- #
def price_ok(price: float, minimum: float) -> FilterResult:
    if price < minimum:
        return FilterResult(False, f"price ${price:.2f} < min ${minimum:.0f}")
    return FilterResult(True)


def spread_ok(spread_pct: float, max_spread_pct: float = 0.003) -> FilterResult:
    if spread_pct > max_spread_pct:
        return FilterResult(False, f"spread {spread_pct*100:.2f}% too wide")
    return FilterResult(True)


def window_ok(cfg, now=None) -> FilterResult:
    """Regular hours, past the opening block, before the no-new-entry cutoff."""
    et = _et_now(now)
    if et.weekday() >= 5:
        return FilterResult(False, "weekend")
    t = et.time()
    from datetime import time as _t
    if cfg.strategy.regular_hours_only and not (_t(9, 30) <= t <= _t(16, 0)):
        return FilterResult(False, "outside regular hours")
    open_block_end = (datetime.combine(et.date(), _t(9, 30)) +
                      timedelta(minutes=cfg.strategy.block_first_minutes)).time()
    if t < open_block_end:
        return FilterResult(False, f"first {cfg.strategy.block_first_minutes}m block")
    try:
        hh, mm = (int(x) for x in cfg.strategy.block_new_entries_after.split(":"))
        if t >= _t(hh, mm):
            return FilterResult(False, f"after {cfg.strategy.block_new_entries_after} cutoff")
    except Exception:
        pass
    return FilterResult(True)


# --- Finnhub-backed filters ------------------------------------------------ #
class FinnhubFilters:
    def __init__(self, api_key: str, news_lookback_min: int = 180,
                 earnings_blackout_days: int = 3, cache_seconds: int = 600, timeout: int = 6):
        self.api_key = api_key or ""
        self.news_lookback = timedelta(minutes=news_lookback_min)
        self.earnings_blackout_days = earnings_blackout_days
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self._news_cache: dict[str, tuple] = {}
        self._earn_cache: dict[str, tuple] = {}

    def _get(self, path: str, params: dict):
        params = {**params, "token": self.api_key}
        url = f"https://finnhub.io/api/v1/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"X-Finnhub-Token": self.api_key})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def news_ok(self, symbol: str, now=None) -> FilterResult:
        if not self.api_key:
            return FilterResult(True)
        now = now or datetime.now(timezone.utc)
        cached = self._news_cache.get(symbol)
        if cached and _time.time() - cached[0] < self.cache_seconds:
            items = cached[1]
        else:
            try:
                items = self._get("company-news", {
                    "symbol": symbol,
                    "from": (now.date() - timedelta(days=1)).isoformat(),
                    "to": now.date().isoformat()})
                items = items if isinstance(items, list) else []
                self._news_cache[symbol] = (_time.time(), items)
            except Exception as exc:  # noqa: BLE001 — fail open
                return FilterResult(True, f"news fetch failed: {exc!r}")
        cutoff = now - self.news_lookback
        for it in items:
            try:
                dt = datetime.fromtimestamp(float(it.get("datetime")), tz=timezone.utc)
            except (TypeError, ValueError):
                continue
            if dt < cutoff:
                continue
            head = (it.get("headline") or "").lower()
            for kw in NEWS_KEYWORDS:
                if kw in head:
                    return FilterResult(False, f"news: '{kw.strip()}'")
        return FilterResult(True)

    def earnings_ok(self, symbol: str, now=None) -> FilterResult:
        if not self.api_key:
            return FilterResult(True)
        now = now or datetime.now(timezone.utc)
        cached = self._earn_cache.get(symbol)
        if cached and _time.time() - cached[0] < self.cache_seconds * 4:
            dates = cached[1]
        else:
            try:
                data = self._get("calendar/earnings", {
                    "symbol": symbol,
                    "from": now.date().isoformat(),
                    "to": (now.date() + timedelta(days=self.earnings_blackout_days + 1)).isoformat()})
                dates = [row.get("date") for row in (data.get("earningsCalendar") or [])]
                self._earn_cache[symbol] = (_time.time(), dates)
            except Exception as exc:  # noqa: BLE001 — fail open
                return FilterResult(True, f"earnings fetch failed: {exc!r}")
        for d in dates:
            try:
                ed = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
            except Exception:
                continue
            gap = (ed - now.date()).days
            if 0 <= gap <= self.earnings_blackout_days:
                return FilterResult(False, f"earnings in {gap}d")
        return FilterResult(True)
