import unittest

import numpy as np
import pandas as pd

from factorlab.core import FACTOR_COLUMNS
from factorlab.simulator import factor_targets, simulate_open_rebalances


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        self.days = pd.bdate_range("2024-01-02", periods=3)
        self.prices = pd.DataFrame([
            {"date": day, "symbol": symbol, "open": price}
            for day, a, b in zip(self.days, [10, 11, 12], [20, 21, 22])
            for symbol, price in (("000001", a), ("000002", b))])
        self.flags = pd.DataFrame([
            {"date": day, "symbol": symbol, "can_buy_open": 1, "can_sell_open": 1}
            for day in self.days for symbol in ("000001", "000002")])

    def test_blocked_sale_carries_position_and_cash_constrains_new_buy(self):
        targets = pd.DataFrame([
            {"trade_date": self.days[0], "symbol": "000001", "target_weight": 1.0},
            {"trade_date": self.days[1], "symbol": "000002", "target_weight": 1.0},
            {"trade_date": self.days[2], "symbol": "000002", "target_weight": 1.0},
        ])
        self.flags.loc[(self.flags.date == self.days[1]) &
                       (self.flags.symbol == "000001"), "can_sell_open"] = 0
        ledger, orders, positions = simulate_open_rebalances(self.prices, self.days, targets, self.flags,
                                                              initial_cash=1000, cost_bps=0)
        self.assertAlmostEqual(ledger.iloc[0]["nav"], 1000)
        self.assertAlmostEqual(ledger.iloc[1]["nav"], 1100)
        self.assertEqual(ledger.iloc[1]["holdings_n"], 1)
        self.assertEqual(ledger.iloc[1]["blocked_orders"], 1)
        self.assertAlmostEqual(ledger.iloc[2]["nav"], 1200)
        self.assertEqual(ledger.iloc[2]["holdings_n"], 1)
        self.assertEqual(orders.loc[orders.status == "blocked", "symbol"].tolist(), ["000001"])
        self.assertEqual(orders.loc[(orders.date == self.days[2]) & (orders.side == "buy"),
                                    "symbol"].tolist(), ["000002"])
        self.assertEqual(positions.loc[positions.date == self.days[1], "symbol"].tolist(), ["000001"])

    def test_missing_open_for_held_stock_needs_explicit_valuation(self):
        targets = pd.DataFrame([{"trade_date": self.days[0], "symbol": "000001",
                                 "target_weight": 1.0},
                                {"trade_date": self.days[1], "symbol": "000002",
                                 "target_weight": 1.0}])
        prices = self.prices.loc[~((self.prices.date == self.days[1]) &
                                   (self.prices.symbol == "000001"))]
        self.flags.loc[(self.flags.date == self.days[1]) &
                       (self.flags.symbol == "000001"), "can_sell_open"] = 0
        with self.assertRaisesRegex(ValueError, "Missing open/valuation"):
            simulate_open_rebalances(prices, self.days, targets, self.flags, cost_bps=0)
        valuation = pd.DataFrame([{"date": self.days[1], "symbol": "000001", "price": 10.5}])
        ledger, _, positions = simulate_open_rebalances(prices, self.days, targets, self.flags,
                                                        valuation=valuation, cost_bps=0)
        self.assertAlmostEqual(ledger.iloc[1]["nav"], 1_050_000)
        self.assertEqual(positions.loc[positions.date == self.days[1], "mark_source"].tolist(), ["valuation"])

    def test_unchanged_halted_position_creates_no_phantom_buy(self):
        targets = pd.DataFrame([{"trade_date": self.days[0], "symbol": "000001", "target_weight": 1.0},
                                {"trade_date": self.days[1], "symbol": "000001", "target_weight": 1.0}])
        prices = self.prices.loc[~((self.prices.date == self.days[1]) &
                                   (self.prices.symbol == "000001"))]
        valuation = pd.DataFrame([{"date": self.days[1], "symbol": "000001", "price": 10.5}])
        ledger, orders, _ = simulate_open_rebalances(prices, self.days, targets, self.flags,
                                                      valuation=valuation, cost_bps=0)
        self.assertEqual(int(ledger.iloc[1]["blocked_orders"]), 0)
        self.assertEqual(len(orders.loc[orders.date == self.days[1]]), 0)

    def test_targets_ignore_future_return_labels(self):
        panel = pd.DataFrame({"date": [self.days[0]] * 5,
                              "entry_date": [self.days[1]] * 5,
                              "symbol": [f"{n:06d}" for n in range(5)],
                              "fwd_ret": [np.nan, -10, 5, 20, 100]})
        for factor in FACTOR_COLUMNS:
            panel[factor] = [1, 2, 3, 4, 5]
        first = factor_targets(panel, self.days[:1], min_stocks=5)
        panel["fwd_ret"] = [100, 20, 5, -10, np.nan]
        second = factor_targets(panel, self.days[:1], min_stocks=5)
        pd.testing.assert_frame_equal(first, second)
        self.assertEqual(first["symbol"].tolist(), ["000004"])


if __name__ == "__main__":
    unittest.main()
