"""Build a forward-only research adjustment chain from BaoStock raw bars."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from factorlab.adjustments import convert_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(convert_dataset(args.dataset), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
