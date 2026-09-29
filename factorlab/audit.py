"""Fail-closed input contract for research runs with auditable data provenance."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .core import load_membership

TIMESTAMP_SUFFIX = re.compile(r"(?:Z|[+-]\d{2}:\d{2})$")
MANIFEST_FIELDS = ("dataset_id", "price_source", "calendar_source", "membership_source",
                   "benchmark_source", "execution_source", "license", "price_basis")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Manifest schema_version must be 1")
    missing = [key for key in MANIFEST_FIELDS if not isinstance(manifest.get(key), str) or not manifest[key].strip()]
    if missing:
        raise ValueError(f"Manifest has empty required fields: {missing}")
    if manifest["price_basis"] != "point_in_time_adjusted":
        raise ValueError("Strict research requires price_basis=point_in_time_adjusted")
    return manifest


def _aware_utc(values: pd.Series, label: str) -> pd.Series:
    raw = values.astype("string")
    if raw.isna().any() or not raw.str.contains(TIMESTAMP_SUFFIX).all():
        raise ValueError(f"{label} requires timezone-aware ISO timestamps for every row")
    parsed = pd.to_datetime(raw, utc=True, errors="raise")
    if parsed.isna().any():
        raise ValueError(f"{label} contains missing timestamps")
    return parsed


def _decision_cutoff_map(calendar: pd.DatetimeIndex) -> dict[pd.Timestamp, pd.Timestamp]:
    return {date: calendar[i + 1].tz_localize("Asia/Shanghai") + pd.Timedelta(hours=9)
            for i, date in enumerate(calendar[:-1])}


def validate_price_availability(data_dir: Path, calendar: pd.DatetimeIndex) -> dict:
    """A daily bar may enter a signal only if published by 09:00 before execution."""
    cutoffs = _decision_cutoff_map(calendar)
    total = 0
    hashes = {}
    for path in sorted(data_dir.glob("*.csv")):
        frame = pd.read_csv(path, usecols=lambda column: column in {"date", "available_at"})
        if not {"date", "available_at"}.issubset(frame.columns):
            raise ValueError(f"{path.name} needs date,available_at in strict mode")
        dates = pd.to_datetime(frame["date"], errors="raise")
        available = _aware_utc(frame["available_at"], f"{path.name}.available_at")
        for date, seen in zip(dates, available):
            if date not in calendar:
                raise ValueError(f"{path.name} has date {date.date()} outside calendar")
            close = date.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)
            deadline = cutoffs.get(date)
            if seen < close or (deadline is not None and seen > deadline):
                raise ValueError(f"{path.name} bar on {date.date()} was unavailable at signal time")
        total += len(frame)
        hashes[path.name] = _hash_file(path)
    if not hashes:
        raise ValueError(f"No price CSV files in {data_dir}")
    return {"price_rows": total, "price_file_sha256": hashes}


def validate_membership(path: Path, calendar: pd.DatetimeIndex,
                        start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    raw = pd.read_csv(path, dtype={"symbol": "string"})
    if "known_at" not in raw.columns:
        raise ValueError("Strict membership requires known_at timezone-aware timestamps")
    membership = load_membership(path)
    known = _aware_utc(raw["known_at"], "membership.known_at")
    cutoffs = _decision_cutoff_map(calendar)
    for date, seen in zip(membership["date"], known):
        if date not in calendar:
            raise ValueError(f"Membership date {date.date()} is outside calendar")
        deadline = cutoffs.get(date)
        if deadline is not None and seen > deadline:
            raise ValueError(f"Membership for {date.date()} was learned after the decision cutoff")
    required_dates = calendar[(calendar >= start) & (calendar <= end)]
    missing = required_dates.difference(pd.DatetimeIndex(membership["date"].unique()))
    if len(missing):
        raise ValueError(f"Membership has no rows for {len(missing)} signal dates, starting {missing[0].date()}")
    return membership


def load_execution_flags(path: Path, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"symbol": "string"})
    required = {"date", "symbol", "can_buy_open", "can_sell_open", "observed_at"}
    if not required.issubset(frame.columns):
        raise ValueError(f"Execution CSV needs {sorted(required)}")
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    frame["symbol"] = frame["symbol"].str.zfill(6)
    if frame.duplicated(["date", "symbol"]).any():
        raise ValueError("Execution CSV contains duplicate date,symbol")
    for column in ("can_buy_open", "can_sell_open"):
        if not frame[column].isin([0, 1]).all():
            raise ValueError(f"{column} must contain only 0 or 1")
        frame[column] = frame[column].astype(bool)
    observed = _aware_utc(frame["observed_at"], "execution.observed_at")
    for date, seen in zip(frame["date"], observed):
        if date not in calendar:
            raise ValueError(f"Execution date {date.date()} is outside calendar")
        market_open = date.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=9, minutes=30)
        if seen > market_open:
            raise ValueError(f"Execution status for {date.date()} was learned after the open")
    return frame[["date", "symbol", "can_buy_open", "can_sell_open"]]


def load_valuation_prices(path: Path, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """Explicit daily marks for held securities without an executable open."""
    frame = pd.read_csv(path, dtype={"symbol": "string"})
    required = {"date", "symbol", "price", "observed_at"}
    if not required.issubset(frame.columns):
        raise ValueError(f"Valuation CSV needs {sorted(required)}")
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    frame["symbol"] = frame["symbol"].str.zfill(6)
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    if frame.duplicated(["date", "symbol"]).any() or not frame["price"].gt(0).all():
        raise ValueError("Valuation needs unique date,symbol and positive prices")
    observed = _aware_utc(frame["observed_at"], "valuation.observed_at")
    for date, seen in zip(frame["date"], observed):
        if date not in calendar:
            raise ValueError(f"Valuation date {date.date()} is outside calendar")
        market_open = date.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=9, minutes=30)
        if seen > market_open:
            raise ValueError(f"Valuation for {date.date()} was learned after the open")
    return frame[["date", "symbol", "price"]]


def validate_member_price_coverage(membership: pd.DataFrame, prices: pd.DataFrame,
                                   execution: pd.DataFrame, start: pd.Timestamp,
                                   end: pd.Timestamp) -> int:
    """Every missing member bar needs an explicit nontradable status."""
    members = membership.loc[membership["date"].between(start, end), ["date", "symbol"]]
    unknown = set(members["symbol"]) - set(prices["symbol"])
    if unknown:
        raise ValueError(f"No price file/history for member symbol {sorted(unknown)[0]}")
    missing = members.merge(prices[["date", "symbol"]], on=["date", "symbol"],
                            how="left", indicator=True, validate="one_to_one")
    missing = missing.loc[missing["_merge"] == "left_only", ["date", "symbol"]]
    if missing.empty:
        return 0
    states = missing.merge(execution, on=["date", "symbol"], how="left", validate="one_to_one")
    invalid = states.loc[states["can_buy_open"].isna() | states["can_sell_open"].isna() |
                         states["can_buy_open"].eq(True) | states["can_sell_open"].eq(True)]
    if not invalid.empty:
        item = invalid.iloc[0]
        raise ValueError(f"Missing member bar lacks nontradable status: {item['symbol']} on {item['date'].date()}")
    return len(missing)


def load_benchmark_returns(path: Path, calendar: pd.DatetimeIndex, horizon: int) -> pd.Series:
    frame = pd.read_csv(path)
    if not {"date", "open"}.issubset(frame.columns):
        raise ValueError("Benchmark CSV needs date,open")
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    if frame["date"].duplicated().any() or not pd.DatetimeIndex(frame["date"]).isin(calendar).all():
        raise ValueError("Benchmark has duplicate or out-of-calendar dates")
    prices = pd.to_numeric(frame.set_index("date")["open"], errors="coerce").reindex(calendar)
    if prices.le(0).any():
        raise ValueError("Benchmark has nonpositive open prices")
    returns = prices.shift(-1 - horizon).div(prices.shift(-1)).sub(1)
    returns.name = "benchmark_ret"
    return returns


def input_fingerprints(paths: dict[str, Path], price_audit: dict) -> dict:
    return {"files_sha256": {key: _hash_file(path) for key, path in paths.items()}, **price_audit}
