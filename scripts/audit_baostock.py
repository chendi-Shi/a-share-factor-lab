"""Audit a BaoStock snapshot and write explicit production acceptance gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from factorlab.readiness import audit_baostock_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--require-production", action="store_true",
                        help="Exit with status 2 while any production gate is unverified")
    args = parser.parse_args()
    report = audit_baostock_dataset(args.dataset)
    target = args.dataset / "readiness.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.require_production and not report["production_ready"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
