"""Data validation, backward-looking factors, and executable return labels."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

FACTOR_COLUMNS = ("mom_60_5", "reversal_5", "low_vol_20", "log_amount_20")
REQUIRED = {"date", "open", "high", "low", "close", "amount"}


def load_prices(data_dir: Path, symbols_json: Path | None = None,
                reject_bad_prices: bool = False) -> pd.DataFrame:
    """Read one CSV per symbol. A snapshot symbols file is *not* PIT membership."""
    symbols = None
    if symbols_json is not None:
        symbols = {str(s).zfill(6) for s in json.loads(symbols_json.read_text(encoding="utf-8"))}
    files = sorted(data_dir.glob("*.csv"))
    if symbols is not None:
        files = [path for path in files if path.stem.zfill(6) in symbols]
    if not files:
        raise ValueError(f"No matching CSV files in {data_dir}")
    frames = []
    bad_price_rows = 0
    for path in files:
        frame = pd.read_csv(path, dtype={"symbol": "string"})
        missing = REQUIRED - set(frame.columns)
        if missing:
            raise ValueError(f"{path.name} is missing {sorted(missing)}")
        frame["symbol"] = path.stem.zfill(6)
        frame["date"] = pd.to_datetime(frame["date"], errors="raise")
        if frame["date"].duplicated().any():
            raise ValueError(f"{path.name} contains duplicate dates")
        for column in ("open", "high", "low", "close", "amount"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        valid_prices = frame[["open", "high", "low", "close"]].gt(0).all(axis=1)
        if reject_bad_prices:
            valid_prices &= frame["high"].ge(frame[["open", "close"]].max(axis=1))
            valid_prices &= frame["low"].le(frame[["open", "close"]].min(axis=1))
            valid_prices &= frame["amount"].ge(0)
        if reject_bad_prices and not valid_prices.all():
            bad = frame.loc[~valid_prices, "date"].iloc[0].date()
            raise ValueError(f"{path.name} has invalid OHLC/amount on {bad}; strict mode refuses to drop rows")
        bad_price_rows += int((~valid_prices).sum())
        frame = frame.loc[valid_prices]
        frames.append(frame[["date", "symbol", "open", "high", "low", "close", "amount"]])
    combined = pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"])
    combined.attrs["dropped_bad_price_rows"] = bad_price_rows
    return combined


def load_membership(path: Path) -> pd.DataFrame:
    """Exact daily, as-of-known membership: columns date,symbol."""
    frame = pd.read_csv(path, dtype={"symbol": "string"})
    if not {"date", "symbol"}.issubset(frame.columns):
        raise ValueError("Membership CSV must contain date,symbol")
    frame = frame[["date", "symbol"]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    frame["symbol"] = frame["symbol"].str.zfill(6)
    if frame.duplicated().any():
        raise ValueError("Membership CSV contains duplicate date,symbol")
    return frame


def load_calendar(path: Path) -> pd.DatetimeIndex:
    frame = pd.read_csv(path)
    if "date" not in frame.columns:
        raise ValueError("Calendar CSV must contain date")
    dates = pd.to_datetime(frame["date"], errors="raise")
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError("Calendar dates must be unique, nonempty and strictly increasing")
    return pd.DatetimeIndex(dates, name="date")


def build_panel(prices: pd.DataFrame, horizon: int = 5,
                calendar: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    """At close t compute factors; enter at open t+1, exit at open t+1+h.

    Every symbol is aligned to the union exchange calendar before shifting.
    A missing bar remains missing, rather than silently shifting the horizon.
    """
    if horizon < 1:
        raise ValueError("horizon must be positive")
    if calendar is None:
        calendar = pd.DatetimeIndex(sorted(prices["date"].unique()), name="date")
    elif not pd.DatetimeIndex(prices["date"].unique()).isin(calendar).all():
        raise ValueError("Price dates contain values absent from the supplied calendar")
    entry_dates = pd.Series(calendar, index=calendar).shift(-1)
    exit_dates = pd.Series(calendar, index=calendar).shift(-1 - horizon)
    frames = []
    for symbol, raw in prices.groupby("symbol", sort=True):
        stock = raw.set_index("date").sort_index().reindex(calendar)
        stock["symbol"] = symbol
        close = stock["close"]
        amount = stock["amount"].where(stock["amount"] > 0)
        ret = close.pct_change(fill_method=None)
        stock["mom_60_5"] = close.shift(5).div(close.shift(60)).sub(1)
        stock["reversal_5"] = 1 - close.div(close.shift(5))
        stock["low_vol_20"] = -ret.rolling(20, min_periods=20).std()
        stock["log_amount_20"] = np.log(amount.rolling(20, min_periods=20).mean())
        stock["fwd_ret"] = stock["open"].shift(-1 - horizon).div(stock["open"].shift(-1)).sub(1)
        stock["entry_open"] = stock["open"].shift(-1)
        stock["exit_open"] = stock["open"].shift(-1 - horizon)
        stock["entry_date"] = entry_dates
        stock["exit_date"] = exit_dates
        frames.append(stock.reset_index()[["date", "symbol", "close", "amount", *FACTOR_COLUMNS,
                                          "entry_date", "entry_open", "exit_date", "exit_open", "fwd_ret"]])
    panel = pd.concat(frames, ignore_index=True)
    numeric = [*FACTOR_COLUMNS, "fwd_ret", "entry_open", "exit_open"]
    panel[numeric] = panel[numeric].replace([np.inf, -np.inf], np.nan)
    return panel


def filter_signals(panel: pd.DataFrame, membership: pd.DataFrame | None = None,
                   min_amount: float = 20_000_000) -> pd.DataFrame:
    """All screens depend only on information available by close t."""
    frame = panel.loc[(panel["close"] >= 2) & (panel["amount"] >= min_amount)].copy()
    if membership is not None:
        frame = frame.merge(membership.assign(_member=True), on=["date", "symbol"], how="inner",
                            validate="one_to_one")
        frame = frame.drop(columns="_member")
    return frame
