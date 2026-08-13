import tempfile
import unittest
from pathlib import Path

import pandas as pd

from massive_research import load_massive


class MassiveResearchTests(unittest.TestCase):
    def test_loads_and_aggregates_shared_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "processed" / "minute" / "SPY"
            folder.mkdir(parents=True)
            times = pd.date_range("2025-01-02 14:30:00+00:00", periods=16, freq="min")
            pd.DataFrame({"timestamp": times, "open": range(16), "high": range(1, 17),
                          "low": range(16), "close": range(1, 17)}).to_parquet(folder / "2025.parquet")
            bars = load_massive(Path(tmp), "SPY", 2025, 2025)
            self.assertEqual(2, len(bars))
            self.assertEqual(0, bars[0].open)
            self.assertEqual(15, bars[0].close)


if __name__ == "__main__":
    unittest.main()
