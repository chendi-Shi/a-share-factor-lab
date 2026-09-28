import unittest

import numpy as np
import pandas as pd

from factorlab.core import build_panel, filter_signals
from factorlab.research import _turnover, newey_west_t, portfolio_periods


class ResearchTimingTests(unittest.TestCase):
    def setUp(self):
        dates = pd.bdate_range("2024-01-01", periods=90)
        rows = []
        for n in range(25):
            for i, date in enumerate(dates):
                price = 10 + n / 10 + i * (0.01 + n / 100_000)
                rows.append({"date": date, "symbol": f"{n:06d}",
                             "open": price, "high": price * 1.01, "low": price * 0.99,
                             "close": price * 1.001, "amount": 50_000_000})
        self.prices = pd.DataFrame(rows)
        self.dates = dates

    def test_signal_uses_past_only_and_label_uses_next_open(self):
        panel = build_panel(self.prices)
        target = panel.loc[(panel.symbol == "000000") & (panel.date == self.dates[65])].iloc[0]
        stock = self.prices.loc[self.prices.symbol == "000000"].reset_index(drop=True)
        self.assertAlmostEqual(target.mom_60_5, stock.close.iloc[60] / stock.close.iloc[5] - 1)
        self.assertAlmostEqual(target.fwd_ret, stock.open.iloc[71] / stock.open.iloc[66] - 1)
        changed = self.prices.copy()
        changed.loc[(changed.symbol == "000000") & (changed.date == self.dates[70]), "close"] *= 3
        changed_panel = build_panel(changed)
        changed_target = changed_panel.loc[(changed_panel.symbol == "000000") &
                                           (changed_panel.date == self.dates[65])].iloc[0]
        self.assertAlmostEqual(target.mom_60_5, changed_target.mom_60_5)

    def test_missing_next_open_is_not_shifted_to_later_trade(self):
        prices = self.prices.loc[~((self.prices.symbol == "000000") &
                                   (self.prices.date == self.dates[66]))]
        panel = build_panel(prices)
        target = panel.loc[(panel.symbol == "000000") & (panel.date == self.dates[65])].iloc[0]
        self.assertTrue(np.isnan(target.fwd_ret))

    def test_supplied_calendar_preserves_market_wide_missing_day(self):
        prices = self.prices.loc[self.prices.date != self.dates[66]]
        panel = build_panel(prices, calendar=pd.DatetimeIndex(self.dates, name="date"))
        target = panel.loc[(panel.symbol == "000000") & (panel.date == self.dates[65])].iloc[0]
        self.assertTrue(np.isnan(target.fwd_ret))

    def test_filter_is_as_of_and_membership_exact(self):
        panel = build_panel(self.prices)
        membership = pd.DataFrame({"date": [self.dates[65]], "symbol": ["000000"]})
        result = filter_signals(panel, membership)
        self.assertEqual(result[["date", "symbol"]].to_records(index=False).tolist(),
                         [(np.datetime64(self.dates[65]), "000000")])

    def test_turnover_counts_entry_and_replacement(self):
        self.assertAlmostEqual(_turnover({}, {"A": 0.5, "B": 0.5}), 1)
        self.assertAlmostEqual(_turnover({"A": 0.5, "B": 0.5}, {"C": 1}), 2)

    def test_portfolio_cost_and_nonoverlapping_periods(self):
        panel = filter_signals(build_panel(self.prices))
        panel = panel.loc[panel.date >= self.dates[65]]
        periods = portfolio_periods(panel, cost_bps=10)
        self.assertGreater(len(periods), 0)
        self.assertAlmostEqual(periods.iloc[0].cost_ret, 0.001)
        self.assertTrue((periods.net_ret <= periods.gross_ret).all())
        dates = sorted(panel.date.unique())
        self.assertEqual(periods.date.iloc[0], dates[0])
        self.assertEqual(periods.date.iloc[1], dates[5])
        self.assertEqual(periods.exit_date.iloc[0], dates[6])

    def test_newey_west_handles_constant_series(self):
        self.assertTrue(np.isnan(newey_west_t(pd.Series([0.1] * 10), 5)))


if __name__ == "__main__":
    unittest.main()
