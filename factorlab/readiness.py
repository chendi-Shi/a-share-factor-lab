"""Integrity and acceptance gates for locally collected BaoStock research data."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .adjustments import adjust_raw_bars
from .core import load_calendar, load_membership


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_baostock_dataset(root: Path) -> dict:
    source_path = root / "source_metadata.json"
    metadata = json.loads(source_path.read_text(encoding="utf-8"))
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    if metadata.get("source") != "BaoStock" or metadata.get("raw_bar_schema_version") != 2:
        raise ValueError("Requires a completed BaoStock schema 2 dataset")
    if any(metadata.get(key) != request.get(key) for key in ("start", "end") if key in metadata):
        raise ValueError("Source metadata disagrees with request")
    if (metadata["membership_start"], metadata["membership_end"]) != (request["start"], request["end"]):
        raise ValueError("Membership range disagrees with request")

    calendar = load_calendar(root / "calendar.csv")
    days = {date.strftime("%Y-%m-%d") for date in calendar}
    if len(days) != metadata["trading_days"]:
        raise ValueError("Calendar count disagrees with source metadata")
    members = load_membership(root / "membership.csv")
    counts = members.groupby("date").size()
    if set(counts.index.strftime("%Y-%m-%d")) != days or not counts.eq(500).all():
        raise ValueError("Membership does not contain exactly 500 members per trading date")
    universe = set(members["symbol"])
    if len(universe) != metadata["historical_universe_symbols"]:
        raise ValueError("Historical universe count disagrees with source metadata")
    observations = pd.read_csv(root / "membership_observations.csv", dtype=str)
    if len(observations) != len(days) or set(observations["date"]) != days:
        raise ValueError("Membership observations do not cover the calendar")
    membership_by_day = {date.strftime("%Y-%m-%d"): set(group["symbol"])
                         for date, group in members.groupby("date")}
    for row in observations.itertuples(index=False):
        if row.member_count != "500":
            raise ValueError(f"Incorrect observed member count on {row.date}")
        cache = root / "membership_daily" / f"{row.date}.csv"
        if sha256(cache) != row.sha256:
            raise ValueError(f"Membership cache hash mismatch on {row.date}")
        daily = pd.read_csv(cache, dtype=str)
        if len(daily) != 500 or set(daily["code"].str[-6:]) != membership_by_day[row.date]:
            raise ValueError(f"Membership cache content mismatch on {row.date}")
        update_dates = set(str(row.source_update_dates).split(";"))
        if update_dates != set(daily["updateDate"]) or any(day > row.date for day in update_dates):
            raise ValueError(f"Invalid source update dates on {row.date}")
        retrieved = pd.Timestamp(row.retrieved_at_utc)
        if retrieved.tzinfo is None:
            raise ValueError(f"Unzoned retrieval time on {row.date}")

    raw_files = sorted((root / "prices_raw").glob("*.csv"))
    selected = {path.stem for path in raw_files}
    if not selected or len(selected) != metadata["downloaded_price_symbols"] or not selected.issubset(universe):
        raise ValueError("Raw price file inventory disagrees with source metadata or membership")
    factor_files = {path.stem for path in (root / "adjust_factors").glob("*.csv")}
    if factor_files != selected:
        raise ValueError("Corporate-action event files do not match price files")
    basic = pd.read_csv(root / "stock_basic.csv", dtype=str).fillna("")
    if set(basic["code"].str[-6:]) != selected or len(basic) != len(selected):
        raise ValueError("Stock basic inventory does not match price files")
    for row in basic.itertuples(index=False):
        if row.outDate and any(day > row.outDate for day in
                               members.loc[members["symbol"] == row.code[-6:], "date"].dt.strftime("%Y-%m-%d")):
            raise ValueError(f"Member appears after delisting: {row.code}")
    for symbol in selected:
        events = pd.read_csv(root / "adjust_factors" / f"{symbol}.csv")
        required_events = {"code", "dividOperateDate", "foreAdjustFactor",
                           "backAdjustFactor", "adjustFactor"}
        if not required_events.issubset(events.columns):
            raise ValueError(f"{symbol}: incomplete corporate-action event columns")
        if not events.empty:
            dates = pd.to_datetime(events["dividOperateDate"], errors="raise")
            if dates.isna().any() or dates.duplicated().any() or \
                    dates.min() < pd.Timestamp(metadata["price_start"]) or \
                    dates.max() > pd.Timestamp(metadata["price_end"]):
                raise ValueError(f"{symbol}: duplicate or out-of-range corporate-action event")
            if not events["code"].astype(str).str[-6:].eq(symbol).all():
                raise ValueError(f"{symbol}: corporate-action event code mismatch")
            for column in ("foreAdjustFactor", "backAdjustFactor", "adjustFactor"):
                if not pd.to_numeric(events[column], errors="coerce").gt(0).all():
                    raise ValueError(f"{symbol}: invalid {column}")

    adjusted_meta_path = root / "research_adjustment_metadata.json"
    adjusted_meta = json.loads(adjusted_meta_path.read_text(encoding="utf-8")) if adjusted_meta_path.exists() else None
    adjusted_complete = adjusted_meta is not None
    if adjusted_meta and adjusted_meta["source_metadata_sha256"] != sha256(source_path):
        raise ValueError("Adjusted prices were built from a different source metadata file")
    raw_dates = set()
    traded_rows = 0
    for path in raw_files:
        frame = pd.read_csv(path, dtype={"symbol": "string"})
        if not frame["symbol"].str.zfill(6).eq(path.stem).all() or not frame["adjustflag"].eq(3).all():
            raise ValueError(f"{path.name}: wrong symbol or raw adjustment flag")
        dates = pd.to_datetime(frame["date"], errors="raise")
        if dates.min() < pd.Timestamp(metadata["price_start"]) or dates.max() > pd.Timestamp(metadata["price_end"]):
            raise ValueError(f"{path.name}: bars outside requested price range")
        research = adjust_raw_bars(frame)
        traded_rows += len(research)
        raw_dates.update((day, path.stem) for day in frame["date"] if day in days)
        if adjusted_meta is not None:
            if adjusted_meta["raw_price_file_sha256"].get(path.name) != sha256(path):
                raise ValueError(f"{path.name}: adjusted series has stale raw input")
            adjusted_path = root / "prices_research_adjusted" / path.name
            if not adjusted_path.exists():
                adjusted_complete = False
            else:
                actual = pd.read_csv(adjusted_path, dtype={"symbol": "string"})
                if len(actual) != len(research) or not actual["date"].eq(research["date"].dt.strftime("%Y-%m-%d")).all():
                    raise ValueError(f"{path.name}: adjusted row dates differ from raw conversion")
                for column in ("open", "high", "low", "close", "raw_close", "adjustment_multiplier"):
                    if not np.allclose(actual[column], research[column], rtol=1e-8, atol=1e-8):
                        raise ValueError(f"{path.name}: adjusted {column} differs from raw conversion")
    if adjusted_meta is not None and adjusted_meta["converted_rows"] != traded_rows:
        raise ValueError("Adjusted row count disagrees with conversion report")

    benchmark = pd.read_csv(root / "benchmark.csv")
    if benchmark["date"].duplicated().any() or not days.issubset(set(benchmark["date"])):
        raise ValueError("Benchmark does not cover every trading date")
    if not pd.to_numeric(benchmark["open"], errors="coerce").gt(0).all():
        raise ValueError("Benchmark has invalid open prices")
    member_pairs = {(date.strftime("%Y-%m-%d"), symbol) for date, symbol in
                    members[["date", "symbol"]].itertuples(index=False, name=None) if symbol in selected}
    missing_member_bars = len(member_pairs - raw_dates)
    gates = {
        "local_integrity": True,
        "full_universe_prices": selected == universe and not metadata["price_sample_only"],
        "member_bars_explained": missing_member_bars == 0,
        "research_adjustment_built": adjusted_complete,
        "historical_publication_times_verified": False,
        "adjustment_vintages_verified": False,
        "open_auction_fillability_verified": False,
        "data_usage_and_publication_rights_verified": False,
    }
    return {"dataset": str(root.resolve()), "trading_days": len(days),
            "historical_universe_symbols": len(universe), "downloaded_price_symbols": len(selected),
            "traded_price_rows": traded_rows, "missing_member_bars_among_downloaded_symbols": missing_member_bars,
            "gates": gates, "production_ready": all(gates.values())}
