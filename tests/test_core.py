import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import config as config_mod
from backtest import Bar, aggregate_hourly, run as run_backtest
from broker import Quote, VirtualBroker
from filters import _et_now
from state import State
from data import Bars
from strategy import TrendPullback


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.env = patch.dict(os.environ, {"DATABASE_URL": "", "STATE_DB": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        os.unlink(self.tmp.name)

    def test_virtual_reset_clears_memory_and_restores_cash(self):
        cfg = config_mod.Config(mode="paper")
        state = State()
        broker = VirtualBroker(cfg, state, quote_fn=lambda _: Quote(99, 100, 99.5))
        broker.submit_bracket("SPY", 1, 100, 95, 110)
        state.reset()
        broker.reset()
        self.assertEqual([], broker.get_positions())
        self.assertEqual(2000, broker.get_account().cash)

    def test_eastern_time_observes_standard_and_daylight_time(self):
        winter = _et_now(datetime(2026, 1, 15, 15, tzinfo=timezone.utc))
        summer = _et_now(datetime(2026, 7, 15, 15, tzinfo=timezone.utc))
        self.assertEqual(10, winter.hour)
        self.assertEqual(11, summer.hour)

    def test_live_mode_is_disabled(self):
        with patch.dict(os.environ, {"AGENT_MODE": "live", "ALPACA_API_KEY": "x",
                                    "ALPACA_SECRET_KEY": "y"}):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                config_mod.load()

    def test_trade_day_uses_new_york_boundary(self):
        state = State()
        # 00:30 UTC on Jan 16 is still Jan 15 in New York.
        state._exec("INSERT INTO trades(ts,side) VALUES(?,?)",
                    ("2026-01-16T00:30:00+00:00", "buy"))
        state.conn.commit()
        now = datetime(2026, 1, 16, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(1, state.trades_today(now))

    def test_hourly_bars_are_anchored_to_market_open(self):
        rows = [
            Bar(datetime(2026, 7, 15, h, m, tzinfo=timezone.utc), 100, 102, 99, close)
            for h, m, close in ((13, 30, 100), (14, 15, 101), (14, 30, 102))
        ]
        bars = aggregate_hourly(rows)
        self.assertEqual(2, len(bars))
        self.assertEqual(9, bars[0].ts.hour)
        self.assertEqual(30, bars[0].ts.minute)
        self.assertEqual(101, bars[0].close)

    def test_flat_market_backtest_has_no_false_edge(self):
        rows = []
        day = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        for d in range(20):
            if (day + timedelta(days=d)).weekday() >= 5:
                continue
            for n in range(26):
                ts = day + timedelta(days=d, minutes=15 * n)
                rows.append(Bar(ts, 100, 100, 100, 100))
        report = run_backtest(rows, "SPY", config_mod.Config())
        self.assertEqual(0, report["trades"])
        self.assertEqual(0, report["return_pct"])
        self.assertEqual(0, report["buy_hold_return_pct"])

    def test_scanner_assessment_explains_non_setup(self):
        strategy = TrendPullback(config_mod.Config())
        flat_1h = Bars([100] * 120, [100] * 120, [100] * 120)
        flat_15m = Bars([100] * 60, [100] * 60, [100] * 60)
        result = strategy.assess("SPY", flat_1h, flat_15m)
        self.assertIsNone(result.setup)
        self.assertFalse(result.checks["uptrend"])
        self.assertTrue(any("missing uptrend" == x for x in result.reasons))


if __name__ == "__main__":
    unittest.main()
