import unittest

import pandas as pd

from factorlab.adjustments import adjust_raw_bars
from factorlab.core import filter_signals


class AdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.raw = pd.DataFrame([
            {"date": "2024-01-02", "symbol": "000001", "open": 10, "high": 11, "low": 10,
             "close": 11, "preclose": 10, "amount": 1e8, "tradestatus": 1},
            {"date": "2024-01-03", "symbol": "000001", "open": 5.5, "high": 5.7, "low": 5.4,
             "close": 5.6, "preclose": 5.5, "amount": 1e8, "tradestatus": 1},
            {"date": "2024-01-04", "symbol": "000001", "open": 5.7, "high": 5.8, "low": 5.6,
             "close": 5.8, "preclose": 5.6, "amount": 1e8, "tradestatus": 1},
        ])

    def test_ex_date_has_no_mechanical_halving_or_future_rescale(self):
        before = adjust_raw_bars(self.raw.iloc[:2])
        after = adjust_raw_bars(self.raw)
        self.assertEqual(before.loc[0, "close"], 11)
        self.assertEqual(after.loc[0, "close"], 11)
        self.assertAlmostEqual(after.loc[1, "close"], 11.2)
        self.assertAlmostEqual(after.loc[1, "adjustment_multiplier"], 2)
        self.assertAlmostEqual(after.loc[1, "raw_close"], 5.6)
        self.assertAlmostEqual(before.loc[1, "close"], after.loc[1, "close"])

    def test_suspension_keeps_previous_close_for_next_event(self):
        suspended = {**self.raw.iloc[1].to_dict(), "date": "2024-01-03",
                     "open": 0, "high": 0, "low": 0, "close": 0,
                     "preclose": 0, "tradestatus": 0}
        resumed = {**self.raw.iloc[1].to_dict(), "date": "2024-01-04"}
        adjusted = adjust_raw_bars(pd.DataFrame([self.raw.iloc[0].to_dict(), suspended, resumed]))
        self.assertEqual(len(adjusted), 2)
        self.assertAlmostEqual(adjusted.iloc[-1]["close"], 11.2)

    def test_invalid_reference_close_fails(self):
        self.raw.loc[1, "preclose"] = 0
        with self.assertRaisesRegex(ValueError, "reference close"):
            adjust_raw_bars(self.raw)

    def test_price_floor_uses_raw_traded_price(self):
        panel = pd.DataFrame({"close": [4.0, 4.0], "raw_close": [1.5, 2.5],
                              "amount": [5e7, 5e7]})
        self.assertEqual(filter_signals(panel)["raw_close"].tolist(), [2.5])


if __name__ == "__main__":
    unittest.main()
