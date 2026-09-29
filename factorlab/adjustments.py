"""Research-only adjustment chain using each day's raw close and ex-date preclose.

The chain is built forward through time. It never rescales an earlier observation
after seeing a later corporate action. Vendor revisions and actual cash flows are
still unknown, so this is not a certified point-in-time or execution price series.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED = {"date", "symbol", "open", "high", "low", "close", "preclose", "amount", "tradestatus"}


def adjust_raw_bars(raw: pd.DataFrame) -> pd.DataFrame:
    missing = REQUIRED - set(raw.columns)
    if missing:
        raise ValueError(f"Raw bars lack {sorted(missing)}")
    bars = raw.copy()
    bars["date"] = pd.to_datetime(bars["date"], errors="raise")
    if bars["date"].isna().any() or bars["date"].duplicated().any() or not bars["date"].is_monotonic_increasing:
        raise ValueError("Raw bar dates must be unique and increasing")
    if bars["symbol"].isna().any() or bars["symbol"].astype(str).nunique() != 1:
        raise ValueError("A raw bar file must contain one symbol")
    for column in ("open", "high", "low", "close", "preclose", "amount", "tradestatus"):
        bars[column] = pd.to_numeric(bars[column], errors="coerce")
    if not bars["tradestatus"].isin([0, 1]).all():
        raise ValueError("tradestatus must be 0 or 1")
    result = []
    previous_close = None
    multiplier = 1.0
    for row in bars.itertuples(index=False):
        if row.tradestatus == 0:
            continue
        values = [row.open, row.high, row.low, row.close, row.amount]
        if not np.isfinite(values).all() or min(values[:4]) <= 0 or row.amount < 0:
            raise ValueError(f"Invalid raw prices or amount on {row.date.date()}")
        if row.high < max(row.open, row.close) or row.low > min(row.open, row.close):
            raise ValueError(f"Invalid OHLC range on {row.date.date()}")
        if previous_close is not None:
            if not np.isfinite(row.preclose) or row.preclose <= 0:
                raise ValueError(f"Missing ex-date reference close on {row.date.date()}")
            step = previous_close / row.preclose
            if not np.isfinite(step) or step <= 0 or step > 20 or step < 0.05:
                raise ValueError(f"Implausible adjustment step on {row.date.date()}: {step}")
            multiplier *= step
        if not np.isfinite(multiplier) or multiplier <= 0:
            raise ValueError(f"Invalid cumulative adjustment on {row.date.date()}")
        result.append({"date": row.date, "symbol": str(row.symbol).zfill(6),
                       "open": row.open * multiplier, "high": row.high * multiplier,
                       "low": row.low * multiplier, "close": row.close * multiplier,
                       "amount": row.amount, "raw_close": row.close,
                       "adjustment_multiplier": multiplier})
        previous_close = row.close
    if not result:
        raise ValueError("No traded bars in raw input")
    return pd.DataFrame(result)


def convert_dataset(source: Path) -> dict:
    metadata_path = source / "source_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("source") != "BaoStock" or metadata.get("raw_bar_schema_version") != 2:
        raise ValueError("Requires a completed BaoStock raw-bar schema version 2 dataset")
    files = sorted((source / "prices_raw").glob("*.csv"))
    if len(files) != metadata.get("downloaded_price_symbols"):
        raise ValueError("Raw price file count disagrees with source metadata")
    output = source / "prices_research_adjusted"
    output.mkdir(parents=True, exist_ok=True)
    report_path = source / "research_adjustment_metadata.json"
    report_path.unlink(missing_ok=True)
    hashes = {}
    count = 0
    for path in files:
        raw = pd.read_csv(path, dtype={"symbol": "string"})
        if not raw["symbol"].str.zfill(6).eq(path.stem).all():
            raise ValueError(f"{path.name} symbol does not match filename")
        adjusted = adjust_raw_bars(raw)
        temporary = output / f"{path.stem}.csv.tmp"
        adjusted.to_csv(temporary, index=False, float_format="%.10f")
        temporary.replace(output / path.name)
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        count += len(adjusted)
    report = {"algorithm": "forward_preclose_chain_v1", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "source_metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
              "raw_price_file_sha256": hashes, "converted_rows": count,
              "price_sample_only": metadata["price_sample_only"], "strict_mode_eligible": False,
              "interpretation": "research proxy; corporate-action cash flows and vendor vintages unverified"}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
