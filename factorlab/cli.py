"""Command line entry point for a transparent A-share factor experiment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .core import build_panel, filter_signals, load_membership, load_prices
from .research import daily_ic, ic_summary, performance, portfolio_periods


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (int, np.integer)):
        return int(value)
    return value


def _fmt(value: float | None, pct: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2%}" if pct else f"{value:.3f}"


def _report(summary: dict) -> str:
    test = summary["portfolio"]["test"]
    lines = ["# Factor research run", "",
             f"Data: `{summary['data_dir']}`",
             f"Signal dates: {summary['start']} to {summary['end']}; test starts {summary['test_start']}.",
             f"Universe mode: **{summary['universe_mode']}**. Data note: {summary['data_note']}", "",
             "## Timing and rules", "",
             "At close t, compute backward-looking factors and an equal-weight percentile-rank score. "
             f"Buy the top fifth at open t+1; exit at open t+1+{summary['horizon']}. "
             f"Rebalance every {summary['horizon']} market dates. "
             f"Charge {summary['cost_bps']} bps per unit of turnover. The benchmark is the same universe, equal weighted.", "",
             "## Out-of-sample diagnostic", "",
             f"Test periods: {test.get('periods', 0)}; net total return: {_fmt(test.get('total_return'), True)}; "
             f"same-universe benchmark: {_fmt(test.get('benchmark_total_return'), True)}; "
             f"Sharpe: {_fmt(test.get('sharpe'))}; max drawdown: {_fmt(test.get('max_drawdown'), True)}.",
             f"Mean traded notional / NAV: {_fmt(test.get('mean_turnover'))}; "
             f"selected positions with missing future opens: {_fmt(test.get('missing_selected_pct'), True)}.", "",
             f"Single-factor Rank IC (Spearman; Newey-West t statistic with {summary['horizon']} lags):", ""]
    for row in summary["ic"]:
        if row["split"] == "test":
            lines.append(f"- {row['factor']}: mean IC {_fmt(row['mean_ic'])}, "
                         f"NW t {_fmt(row['nw_t'])}, {row['days']} dates")
    lines += ["", "## Interpretation limits", "",
              ("- Snapshot universe membership causes survivorship and selection bias. "
               "Use an as-of daily membership CSV before claiming historical investability."
               if summary["universe_mode"] == "snapshot / biased" else
               "- Synthetic data are only a software demonstration; results have no market meaning."
               if summary["universe_mode"].startswith("synthetic") else
               "- Audit the supplied membership timestamps and completeness before interpreting results."),
              "- Verify price-adjustment conventions, corporate actions and actual execution "
              "rules with an audited point-in-time feed before using external market data.",
              "- Missing future open bars are valued at zero for the period. Suspensions, "
              "limit-up/limit-down, slippage, market impact, fees and taxes are not fully simulated.",
              "- Factors were specified before reading test results. This run is an educational "
              "research diagnostic, not a production strategy or evidence of deployable alpha.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--symbols-json", type=Path, help="Snapshot symbol filter; NOT historical membership")
    parser.add_argument("--membership-csv", type=Path, help="Exact daily point-in-time date,symbol membership")
    parser.add_argument("--synthetic", action="store_true", help="Label output as synthetic demonstration data")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-08-18")
    parser.add_argument("--test-start", default="2025-01-01")
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--cost-bps", type=float, default=10)
    parser.add_argument("--min-amount", type=float, default=20_000_000)
    parser.add_argument("--min-stocks", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/latest"))
    args = parser.parse_args()
    start, end, test_start = (pd.Timestamp(s) for s in (args.start, args.end, args.test_start))
    if not start < test_start <= end:
        parser.error("Require start < test-start <= end")
    if args.horizon < 1:
        parser.error("horizon must be positive")
    prices = load_prices(args.data_dir, args.symbols_json)
    bad_price_rows = prices.attrs["dropped_bad_price_rows"]
    panel = build_panel(prices, args.horizon)
    membership = load_membership(args.membership_csv) if args.membership_csv else None
    panel = filter_signals(panel, membership, args.min_amount)
    panel = panel.loc[panel["date"].between(start, end)].copy()
    ic = daily_ic(panel, args.min_stocks)
    train_panel = panel.loc[panel["date"] < test_start]
    test_panel = panel.loc[panel["date"] >= test_start]
    train_periods = portfolio_periods(train_panel, args.horizon, args.cost_bps, args.min_stocks)
    train_periods = train_periods.loc[train_periods["exit_date"] < test_start]
    test_periods = portfolio_periods(test_panel, args.horizon, args.cost_bps, args.min_stocks)
    if test_periods.empty:
        raise ValueError("No test periods; check date range, symbol coverage and min-stocks")
    if args.synthetic:
        universe_mode = "synthetic demo; no market inference"
        data_note = "Generated by examples/make_demo_data.py; no real price data."
    elif membership is not None:
        universe_mode = "daily PIT membership supplied"
        data_note = "User-supplied daily CSV; verify adjustment and provenance."
    elif args.symbols_json is not None:
        universe_mode = "snapshot / biased"
        data_note = "User-supplied daily CSV with snapshot symbol selection."
    else:
        universe_mode = "unverified universe / biased"
        data_note = "User-supplied daily CSV without PIT membership."
    summary = _clean({"data_dir": str(args.data_dir.resolve()),
                      "universe_mode": universe_mode, "data_note": data_note,
                      "start": args.start, "end": args.end, "test_start": args.test_start,
                      "horizon": args.horizon, "cost_bps": args.cost_bps,
                      "dropped_bad_price_rows": bad_price_rows,
                      "symbols": int(panel["symbol"].nunique()), "signal_rows": int(len(panel)),
                      "ic": ic_summary(ic, test_start, args.horizon),
                      "portfolio": {"train": performance(train_periods, args.horizon),
                                    "test": performance(test_periods, args.horizon)}})
    args.output_dir.mkdir(parents=True, exist_ok=True)
    ic.to_csv(args.output_dir / "ic_daily.csv", index=False)
    train_periods.assign(split="train").pipe(
        lambda train: pd.concat([train, test_periods.assign(split="test")], ignore_index=True)
    ).to_csv(args.output_dir / "portfolio_periods.csv", index=False)
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "report.md").write_text(_report(summary), encoding="utf-8")
    print(_report(summary))


if __name__ == "__main__":
    main()
