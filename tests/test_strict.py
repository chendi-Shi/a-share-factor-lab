import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.audit import (load_benchmark_returns, load_execution_flags,
                             load_manifest, load_valuation_prices, validate_member_price_coverage,
                             validate_membership, validate_price_availability)
from factorlab.core import build_panel, filter_signals, load_calendar, load_prices
from factorlab.research import portfolio_periods


class StrictResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "prices"
        self.data.mkdir()
        self.dates = pd.bdate_range("2024-01-01", periods=90)
        self.calendar = self.root / "calendar.csv"
        pd.DataFrame({"date": self.dates}).to_csv(self.calendar, index=False)
        membership = []
        execution = []
        for date in self.dates:
            for n in range(25):
                symbol = f"{n:06d}"
                membership.append({"date": date, "symbol": symbol,
                                   "known_at": date.strftime("%Y-%m-%dT08:00:00+08:00")})
                execution.append({"date": date, "symbol": symbol,
                                  "can_buy_open": 1, "can_sell_open": 1,
                                  "observed_at": date.strftime("%Y-%m-%dT09:30:00+08:00")})
        self.membership = self.root / "membership.csv"
        self.execution = self.root / "execution.csv"
        pd.DataFrame(membership).to_csv(self.membership, index=False)
        pd.DataFrame(execution).to_csv(self.execution, index=False)
        for n in range(25):
            close = 20 + n / 10 + np.arange(len(self.dates)) * (0.02 + n / 100_000)
            frame = pd.DataFrame({"date": self.dates, "symbol": f"{n:06d}",
                                  "open": close, "high": close * 1.01,
                                  "low": close * 0.99, "close": close,
                                  "amount": 50_000_000,
                                  "available_at": [d.strftime("%Y-%m-%dT16:00:00+08:00")
                                                   for d in self.dates]})
            frame.to_csv(self.data / f"{n:06d}.csv", index=False)
        self.benchmark = self.root / "benchmark.csv"
        pd.DataFrame({"date": self.dates,
                      "open": 1000 + np.arange(len(self.dates))}).to_csv(self.benchmark, index=False)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"dataset_id": "synthetic-qa-1",
                                             "schema_version": 1,
                                             "price_source": "test fixture", "calendar_source": "test fixture",
                                             "membership_source": "test fixture", "benchmark_source": "test fixture",
                                             "execution_source": "test fixture", "license": "generated",
                                             "price_basis": "point_in_time_adjusted"}), encoding="utf-8")

    def test_strict_cli_writes_audit_and_uses_external_index(self):
        output = self.root / "output"
        cmd = [sys.executable, "-m", "factorlab.cli", "--strict", "--synthetic",
               "--data-dir", str(self.data), "--calendar-csv", str(self.calendar),
               "--membership-csv", str(self.membership), "--execution-csv", str(self.execution),
               "--benchmark-csv", str(self.benchmark), "--manifest", str(self.manifest),
               "--start", str(self.dates[65].date()), "--end", str(self.dates[76].date()),
               "--test-start", str(self.dates[70].date()), "--min-stocks", "20",
               "--output-dir", str(output)]
        run = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(len(audit["run_id_sha256"]), 64)
        self.assertEqual(len(audit["code_sha256"]), 64)
        self.assertEqual(len(audit["price_file_sha256"]), 25)
        self.assertTrue(summary["strict_validation"])
        self.assertEqual(summary["benchmark_mode"], "external index")

    def test_event_research_cli_writes_cash_and_order_ledger(self):
        output = self.root / "event_output"
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_event_research.py"
        cmd = [sys.executable, str(script), "--synthetic",
               "--data-dir", str(self.data), "--calendar-csv", str(self.calendar),
               "--membership-csv", str(self.membership), "--execution-csv", str(self.execution),
               "--benchmark-csv", str(self.benchmark), "--manifest", str(self.manifest),
               "--start", str(self.dates[65].date()), "--end", str(self.dates[76].date()),
               "--output-dir", str(output)]
        run = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        completion = json.loads((output / "complete.json").read_text(encoding="utf-8"))
        ledger = pd.read_csv(output / "ledger.csv")
        orders = pd.read_csv(output / "orders.csv")
        positions = pd.read_csv(output / "positions.csv")
        self.assertEqual(summary["engine"], "daily_open_fractional_units_v1")
        self.assertTrue(summary["synthetic"])
        self.assertFalse(summary["production_approved"])
        self.assertEqual(len(completion["products_sha256"]), 6)
        self.assertEqual(completion["products_sha256"]["ledger.csv"],
                         hashlib.sha256((output / "ledger.csv").read_bytes()).hexdigest())
        self.assertTrue(ledger["cash"].ge(0).all())
        self.assertGreater(len(orders), 0)
        self.assertGreater(len(positions), 0)

    def test_rejects_late_bar_and_missing_membership(self):
        frame = pd.read_csv(self.data / "000000.csv")
        frame.loc[0, "available_at"] = self.dates[1].strftime("%Y-%m-%dT10:00:00+08:00")
        frame.to_csv(self.data / "000000.csv", index=False)
        with self.assertRaisesRegex(ValueError, "unavailable at signal time"):
            validate_price_availability(self.data, load_calendar(self.calendar))
        members = pd.read_csv(self.membership, dtype={"symbol": "string"})
        members = members.loc[members.date != str(self.dates[65].date())]
        members.to_csv(self.membership, index=False)
        with self.assertRaisesRegex(ValueError, "no rows"):
            validate_membership(self.membership, load_calendar(self.calendar),
                                self.dates[65], self.dates[76])

    def test_rejects_blocked_fill_and_missing_future_price(self):
        calendar = load_calendar(self.calendar)
        prices = load_prices(self.data, reject_bad_prices=True)
        panel = filter_signals(build_panel(prices, calendar=calendar),
                               validate_membership(self.membership, calendar,
                                                   self.dates[65], self.dates[76]))
        panel = panel.loc[panel.date >= self.dates[65]]
        flags = load_execution_flags(self.execution, calendar)
        benchmark = load_benchmark_returns(self.benchmark, calendar, 5)
        dates = calendar[(calendar >= self.dates[65]) & (calendar <= self.dates[66])]
        flags.loc[flags.date == self.dates[66], "can_buy_open"] = False
        with self.assertRaisesRegex(ValueError, "Blocked can_buy_open"):
            portfolio_periods(panel, strict=True, execution=flags,
                              benchmark_returns=benchmark, rebalance_dates=dates)
        flags.loc[flags.date == self.dates[66], "can_buy_open"] = True
        panel.loc[panel.date == self.dates[65], "fwd_ret"] = np.nan
        with self.assertRaisesRegex(ValueError, "Missing future open"):
            portfolio_periods(panel, strict=True, execution=flags,
                              benchmark_returns=benchmark, rebalance_dates=dates)

    def test_rejects_unverified_manifest(self):
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        data["price_basis"] = "latest_adjusted"
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "point_in_time_adjusted"):
            load_manifest(self.manifest)

    def test_rejects_mislabeled_price_file(self):
        path = self.data / "000000.csv"
        frame = pd.read_csv(path, dtype={"symbol": "string"})
        frame.loc[0, "symbol"] = "999999"
        frame.to_csv(path, index=False)
        with self.assertRaisesRegex(ValueError, "symbol column does not match"):
            load_prices(self.data, reject_bad_prices=True)

    def test_valuation_requires_preopen_observation(self):
        path = self.root / "valuation.csv"
        pd.DataFrame([{"date": self.dates[65], "symbol": "000000", "price": 20,
                       "observed_at": self.dates[65].strftime("%Y-%m-%dT10:00:00+08:00")}]).to_csv(path, index=False)
        with self.assertRaisesRegex(ValueError, "after the open"):
            load_valuation_prices(path, load_calendar(self.calendar))

    def test_missing_member_bar_needs_explicit_suspension(self):
        prices = load_prices(self.data, reject_bad_prices=True)
        prices = prices.loc[~((prices.symbol == "000000") & (prices.date == self.dates[65]))]
        calendar = load_calendar(self.calendar)
        members = validate_membership(self.membership, calendar, self.dates[65], self.dates[76])
        flags = load_execution_flags(self.execution, calendar)
        with self.assertRaisesRegex(ValueError, "lacks nontradable status"):
            validate_member_price_coverage(members, prices, flags, self.dates[65], self.dates[76])
        flags.loc[(flags.symbol == "000000") & (flags.date == self.dates[65]),
                  ["can_buy_open", "can_sell_open"]] = False
        self.assertEqual(validate_member_price_coverage(members, prices, flags,
                                                        self.dates[65], self.dates[76]), 1)
        with self.assertRaisesRegex(ValueError, "No price file/history"):
            validate_member_price_coverage(members, prices.loc[prices.symbol != "000000"],
                                           flags, self.dates[65], self.dates[76])


if __name__ == "__main__":
    unittest.main()
