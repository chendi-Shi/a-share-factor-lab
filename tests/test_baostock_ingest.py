"""Local ingestion validation; live BaoStock is intentionally absent from CI."""

import unittest

from scripts.fetch_baostock import six_digit, validate_bars, validate_members


class BaoStockIngestTests(unittest.TestCase):
    def test_membership_rejects_incomplete_or_duplicate_index(self):
        rows = [{"code": f"sh.{number:06d}", "updateDate": "2019-12-30"}
                for number in range(500)]
        self.assertEqual(len(validate_members("2020-01-02", rows)), 500)
        with self.assertRaisesRegex(ValueError, "expected 500"):
            validate_members("2020-01-02", rows[:-1])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_members("2020-01-02", rows[:-1] + [rows[0]])
        with self.assertRaisesRegex(ValueError, "future"):
            validate_members("2020-01-02", rows[:-1] + [{**rows[-1], "updateDate": "2020-01-03"}])

    def test_prices_reject_duplicate_and_wrong_security(self):
        good = [{"date": "2020-01-02", "code": "sh.600260"},
                {"date": "2020-01-03", "code": "sh.600260"}]
        self.assertEqual(validate_bars("sh.600260", good), good)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_bars("sh.600260", good + good[-1:])
        with self.assertRaisesRegex(ValueError, "mismatched"):
            validate_bars("sh.600260", [good[0], {**good[1], "code": "sh.600261"}])

    def test_code_normalization_rejects_non_equity_identifiers(self):
        self.assertEqual(six_digit("sz.000001"), "000001")
        with self.assertRaises(ValueError):
            six_digit("600260")


if __name__ == "__main__":
    unittest.main()
