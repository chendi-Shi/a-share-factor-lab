import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from factorlab.adjustments import convert_dataset
from factorlab.readiness import audit_baostock_dataset, sha256


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        day = "2024-01-02"
        codes = [f"sh.{600000 + index:06d}" for index in range(500)]
        (self.root / "request.json").write_text(json.dumps(
            {"schema_version": 2, "start": day, "end": day, "lookback_days": 0,
             "symbols": [codes[0]]}), encoding="utf-8")
        (self.root / "source_metadata.json").write_text(json.dumps(
            {"source": "BaoStock", "raw_bar_schema_version": 2,
             "membership_start": day, "membership_end": day,
             "trading_days": 1, "historical_universe_symbols": 500,
             "downloaded_price_symbols": 1, "price_sample_only": True,
             "price_start": day, "price_end": day}), encoding="utf-8")
        pd.DataFrame({"date": [day]}).to_csv(self.root / "calendar.csv", index=False)
        pd.DataFrame({"date": [day] * 500,
                      "symbol": [code[-6:] for code in codes]}).to_csv(self.root / "membership.csv", index=False)
        daily = self.root / "membership_daily" / f"{day}.csv"
        daily.parent.mkdir()
        pd.DataFrame({"updateDate": ["2024-01-01"] * 500,
                      "code": codes}).to_csv(daily, index=False)
        pd.DataFrame([{"date": day, "source_update_dates": "2024-01-01",
                       "member_count": 500, "retrieved_at_utc": "2024-01-02T18:00:00+08:00",
                       "sha256": sha256(daily)}]).to_csv(self.root / "membership_observations.csv", index=False)
        price = self.root / "prices_raw" / "600000.csv"
        price.parent.mkdir()
        pd.DataFrame([{"date": day, "symbol": "600000", "open": 10, "high": 11,
                       "low": 10, "close": 11, "preclose": 10, "amount": 1e8,
                       "tradestatus": 1, "adjustflag": 3}]).to_csv(price, index=False)
        adjustment = self.root / "adjust_factors" / "600000.csv"
        adjustment.parent.mkdir()
        pd.DataFrame(columns=["code", "dividOperateDate", "foreAdjustFactor",
                              "backAdjustFactor", "adjustFactor"]).to_csv(adjustment, index=False)
        pd.DataFrame([{"code": codes[0], "ipoDate": "2000-01-01", "outDate": "",
                       "type": 1, "status": 1}]).to_csv(self.root / "stock_basic.csv", index=False)
        pd.DataFrame([{"date": day, "open": 100}]).to_csv(self.root / "benchmark.csv", index=False)
        convert_dataset(self.root)

    def test_partial_realistic_dataset_passes_integrity_but_not_production(self):
        report = audit_baostock_dataset(self.root)
        self.assertTrue(report["gates"]["local_integrity"])
        self.assertTrue(report["gates"]["research_adjustment_built"])
        self.assertFalse(report["gates"]["full_universe_prices"])
        self.assertFalse(report["production_ready"])

    def test_tampered_constituent_cache_fails_audit(self):
        cache = self.root / "membership_daily" / "2024-01-02.csv"
        cache.write_text(cache.read_text(encoding="utf-8") + "bad\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            audit_baostock_dataset(self.root)


if __name__ == "__main__":
    unittest.main()
