"""Cross-sectional IC, nonoverlapping portfolio periods, and explicit costs."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .core import FACTOR_COLUMNS


def newey_west_t(values: pd.Series, lags: int) -> float:
    """t statistic for a mean with Bartlett-weighted serial covariance."""
    x = values.dropna().to_numpy(dtype=float)
    n = len(x)
    if n < 3:
        return float("nan")
    centered = x - x.mean()
    long_run = np.dot(centered, centered) / n
    for lag in range(1, min(lags, n - 1) + 1):
        cov = np.dot(centered[lag:], centered[:-lag]) / n
        long_run += 2 * (1 - lag / (lags + 1)) * cov
    se = math.sqrt(max(long_run, 0) / n)
    return float(x.mean() / se) if se > 0 else float("nan")


def daily_ic(panel: pd.DataFrame, min_stocks: int = 20) -> pd.DataFrame:
    rows = []
    for date, group in panel.groupby("date", sort=True):
        for factor in FACTOR_COLUMNS:
            valid = group[[factor, "fwd_ret"]].dropna()
            if len(valid) < min_stocks or valid[factor].nunique() < 2 or valid["fwd_ret"].nunique() < 2:
                continue
            factor_ranks = valid[factor].rank(method="average")
            return_ranks = valid["fwd_ret"].rank(method="average")
            rows.append({"date": date, "exit_date": group["exit_date"].iloc[0],
                         "factor": factor, "n": len(valid),
                         "rank_ic": factor_ranks.corr(return_ranks)})
    return pd.DataFrame(rows, columns=["date", "exit_date", "factor", "n", "rank_ic"])


def ic_summary(ic: pd.DataFrame, test_start: pd.Timestamp, horizon: int) -> list[dict]:
    output = []
    for factor in FACTOR_COLUMNS:
        factor_ic = ic.loc[ic["factor"] == factor].sort_values("date")
        for split, part in (("train", factor_ic.loc[factor_ic["exit_date"] < test_start]),
                            ("test", factor_ic.loc[factor_ic["date"] >= test_start])):
            values = part["rank_ic"]
            output.append({"factor": factor, "split": split, "days": int(len(values)),
                           "mean_ic": float(values.mean()) if len(values) else None,
                           "ic_ir": float(values.mean() / values.std(ddof=1)) if len(values) > 1 and values.std(ddof=1) > 0 else None,
                           "nw_t": newey_west_t(values, horizon) if len(values) else None})
    return output


def _weights(symbols: pd.Series) -> dict[str, float]:
    return {str(symbol): 1.0 / len(symbols) for symbol in symbols}


def _turnover(previous: dict[str, float], current: dict[str, float]) -> float:
    return sum(abs(current.get(s, 0) - previous.get(s, 0)) for s in previous.keys() | current.keys())


def portfolio_periods(panel: pd.DataFrame, horizon: int = 5, cost_bps: float = 10,
                      min_stocks: int = 20, *, strict: bool = False,
                      execution: pd.DataFrame | None = None,
                      benchmark_returns: pd.Series | None = None,
                      rebalance_dates: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    """Equal-weight top fifth; signals at close t, execution at t+1 open.

    Missing future bars are valued as zero period return and counted explicitly.
    This is a research proxy, not an exchange simulator.
    """
    if cost_bps < 0:
        raise ValueError("cost_bps must be nonnegative")
    if strict and (execution is None or benchmark_returns is None or rebalance_dates is None):
        raise ValueError("Strict mode needs execution flags, external benchmark and rebalance calendar")
    dates = sorted(rebalance_dates) if rebalance_dates is not None else sorted(panel["date"].unique())
    execution_index = execution.set_index(["date", "symbol"]) if execution is not None else None
    rows = []
    previous: dict[str, float] = {}
    for date in dates[::horizon]:
        group = panel.loc[panel["date"] == date].dropna(subset=list(FACTOR_COLUMNS)).copy()
        if len(group) < min_stocks:
            if strict:
                raise ValueError(f"Only {len(group)} eligible stocks on {pd.Timestamp(date).date()}")
            continue
        if pd.isna(group["exit_date"].iloc[0]):
            if strict:
                raise ValueError(f"No complete holding horizon after {pd.Timestamp(date).date()}")
            continue
        for factor in FACTOR_COLUMNS:
            group[f"rank_{factor}"] = group[factor].rank(pct=True, method="average")
        group["score"] = group[[f"rank_{factor}" for factor in FACTOR_COLUMNS]].mean(axis=1)
        group = group.sort_values(["score", "symbol"], ascending=[False, True])
        n_top = max(1, math.ceil(len(group) * 0.2))
        top = group.head(n_top)
        if strict:
            missing = top.loc[top["fwd_ret"].isna()]
            if not missing.empty:
                raise ValueError(f"Missing future open for selected {missing.iloc[0]['symbol']} on {pd.Timestamp(date).date()}")
        current = _weights(top["symbol"])
        if strict:
            trade_date = top["entry_date"].iloc[0]
            for symbol in previous.keys() | current.keys():
                delta = current.get(symbol, 0) - previous.get(symbol, 0)
                if abs(delta) < 1e-10:
                    continue
                flag = "can_buy_open" if delta > 0 else "can_sell_open"
                key = (trade_date, symbol)
                if key not in execution_index.index:
                    raise ValueError(f"Missing {flag} for {symbol} on {trade_date.date()}")
                if not bool(execution_index.loc[key, flag]):
                    raise ValueError(f"Blocked {flag} for {symbol} on {trade_date.date()}; fill model required")
        turnover = _turnover(previous, current)
        realized = top["fwd_ret"].fillna(0).to_numpy(dtype=float)
        gross = float(realized.mean())
        if benchmark_returns is not None:
            benchmark = float(benchmark_returns.loc[date])
            if not np.isfinite(benchmark):
                raise ValueError(f"Missing benchmark holding return on {pd.Timestamp(date).date()}")
        else:
            benchmark = float(group["fwd_ret"].fillna(0).mean())
        rows.append({"date": date, "exit_date": top["exit_date"].iloc[0],
                     "universe_n": len(group), "selected_n": len(top),
                     "missing_selected": int(top["fwd_ret"].isna().sum()),
                     "gross_ret": gross, "benchmark_ret": benchmark,
                     "turnover": turnover, "cost_ret": turnover * cost_bps / 10_000,
                     "net_ret": gross - turnover * cost_bps / 10_000})
        # At the next rebalance, positions have drifted with their realized returns.
        if 1 + gross > 0:
            previous = {str(symbol): float((1 + ret) / (n_top * (1 + gross)))
                        for symbol, ret in zip(top["symbol"], realized)}
        else:
            previous = {}
    return pd.DataFrame(rows)


def performance(periods: pd.DataFrame, horizon: int = 5) -> dict:
    if periods.empty:
        return {"periods": 0}
    net = periods["net_ret"]
    benchmark = periods["benchmark_ret"]
    wealth = (1 + net).cumprod()
    wealth_with_cash = pd.concat([pd.Series([1.0]), wealth.reset_index(drop=True)], ignore_index=True)
    periods_per_year = 252 / horizon
    std = net.std(ddof=1)
    return {"periods": int(len(periods)),
            "total_return": float(wealth.iloc[-1] - 1),
            "annualized_return": float(wealth.iloc[-1] ** (periods_per_year / len(periods)) - 1) if wealth.iloc[-1] > 0 else None,
            "benchmark_total_return": float((1 + benchmark).prod() - 1),
            "sharpe": float(net.mean() / std * math.sqrt(periods_per_year)) if len(net) > 1 and std > 0 else None,
            "max_drawdown": float((wealth_with_cash / wealth_with_cash.cummax() - 1).min()),
            "mean_turnover": float(periods["turnover"].mean()),
            "missing_selected_pct": float(periods["missing_selected"].sum() / periods["selected_n"].sum())}
