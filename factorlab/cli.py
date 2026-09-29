"""Command line entry point for a transparent A-share factor experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .audit import (input_fingerprints, load_benchmark_returns, load_execution_flags,
                    load_manifest, validate_member_price_coverage, validate_membership,
                    validate_price_availability)
from .core import FACTOR_COLUMNS, build_panel, filter_signals, load_calendar, load_membership, load_prices
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
             f"Charge {summary['cost_bps']} bps per unit of turnover. "
             f"Benchmark: {summary['benchmark_mode']}.", "",
             "## Out-of-sample diagnostic", "",
             f"Test periods: {test.get('periods', 0)}; net total return: {_fmt(test.get('total_return'), True)}; "
             f"{summary['benchmark_mode']} benchmark: {_fmt(test.get('benchmark_total_return'), True)}; "
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
              ("- Strict mode rejects missing future opens and blocked selected trades. "
               "A full order and position simulator is still required."
               if summary["strict_validation"] else
               "- Missing future open bars are valued at zero for the period. Suspensions, "
               "limit-up/limit-down, slippage, market impact, fees and taxes are not fully simulated."),
              "- Factors were specified before reading test results. This run is an educational "
              "research diagnostic, not a production strategy or evidence of deployable alpha.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--symbols-json", type=Path, help="Snapshot symbol filter; NOT historical membership")
    parser.add_argument("--membership-csv", type=Path, help="Exact daily point-in-time date,symbol membership")
    parser.add_argument("--calendar-csv", type=Path, help="Official trading calendar with date column")
    parser.add_argument("--benchmark-csv", type=Path, help="External index series with date,open")
    parser.add_argument("--execution-csv", type=Path, help="Open-auction buy/sell status by date,symbol")
    parser.add_argument("--manifest", type=Path, help="Data provenance manifest; required in strict mode")
    parser.add_argument("--strict", action="store_true", help="Fail closed on unverified or unexecutable research input")
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
    if args.strict:
        missing = [name for name in ("membership_csv", "calendar_csv", "benchmark_csv", "execution_csv", "manifest")
                   if getattr(args, name) is None]
        if missing:
            parser.error(f"--strict requires {', '.join('--' + x.replace('_', '-') for x in missing)}")
        if args.symbols_json is not None:
            parser.error("--strict refuses snapshot --symbols-json; provide daily membership")
    calendar = load_calendar(args.calendar_csv) if args.calendar_csv else None
    audit = None
    if args.strict:
        manifest = load_manifest(args.manifest)
        price_audit = validate_price_availability(args.data_dir, calendar)
        paths = {"manifest": args.manifest, "calendar": args.calendar_csv,
                 "membership": args.membership_csv, "benchmark": args.benchmark_csv,
                 "execution": args.execution_csv}
        audit = {"manifest": manifest, **input_fingerprints(paths, price_audit)}
    prices = load_prices(args.data_dir, args.symbols_json, reject_bad_prices=args.strict)
    bad_price_rows = prices.attrs["dropped_bad_price_rows"]
    panel = build_panel(prices, args.horizon, calendar)
    membership = (validate_membership(args.membership_csv, calendar, start, end) if args.strict
                  else load_membership(args.membership_csv) if args.membership_csv else None)
    execution = load_execution_flags(args.execution_csv, calendar) if args.strict else None
    benchmark_returns = load_benchmark_returns(args.benchmark_csv, calendar, args.horizon) if args.strict else None
    if args.strict:
        audit["missing_member_bars_with_nontradable_status"] = validate_member_price_coverage(
            membership, prices, execution, start, end)
    panel = filter_signals(panel, membership, args.min_amount)
    panel = panel.loc[panel["date"].between(start, end)].copy()
    if args.strict:
        usable = panel.dropna(subset=list(FACTOR_COLUMNS))
        missing_returns = usable.loc[usable["fwd_ret"].isna()]
        if not missing_returns.empty:
            item = missing_returns.iloc[0]
            raise ValueError(f"Missing future open for eligible {item['symbol']} on {item['date'].date()}")
    ic = daily_ic(panel, args.min_stocks)
    train_panel = panel.loc[panel["date"] < test_start]
    test_panel = panel.loc[panel["date"] >= test_start]
    strict_options = {"strict": args.strict, "execution": execution, "benchmark_returns": benchmark_returns}
    train_dates = calendar[(calendar >= start) & (calendar < test_start)] if args.strict else None
    test_dates = calendar[(calendar >= test_start) & (calendar <= end)] if args.strict else None
    train_periods = portfolio_periods(train_panel, args.horizon, args.cost_bps, args.min_stocks,
                                      rebalance_dates=train_dates, **strict_options)
    train_periods = train_periods.loc[train_periods["exit_date"] < test_start]
    test_periods = portfolio_periods(test_panel, args.horizon, args.cost_bps, args.min_stocks,
                                     rebalance_dates=test_dates, **strict_options)
    if test_periods.empty:
        raise ValueError("No test periods; check date range, symbol coverage and min-stocks")
    if args.synthetic:
        universe_mode = "synthetic demo; no market inference"
        data_note = "Generated by examples/make_demo_data.py; no real price data."
    elif args.strict:
        universe_mode = "strict daily membership contract passed"
        data_note = "Provenance fields and timestamps passed software checks; vendor truth is not independently certified."
    elif membership is not None:
        universe_mode = "daily membership supplied; timestamps unaudited"
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
                      "strict_validation": args.strict,
                      "benchmark_mode": "external index" if args.strict else "same-universe equal weight",
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
    if audit is not None:
        code_files = [Path(__file__), Path(__file__).with_name("core.py"),
                      Path(__file__).with_name("research.py"), Path(__file__).with_name("audit.py")]
        audit["code_sha256"] = hashlib.sha256(b"".join(path.read_bytes() for path in code_files)).hexdigest()
        run_payload = json.dumps({"audit": audit, "parameters": {"start": args.start,
                                "end": args.end, "test_start": args.test_start,
                                "horizon": args.horizon, "cost_bps": args.cost_bps,
                                "min_amount": args.min_amount, "min_stocks": args.min_stocks,
                                "synthetic": args.synthetic, "strict": args.strict}},
                                 ensure_ascii=False, sort_keys=True)
        audit["run_id_sha256"] = hashlib.sha256(run_payload.encode("utf-8")).hexdigest()
        (args.output_dir / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "report.md").write_text(_report(summary), encoding="utf-8")
    print(_report(summary))


if __name__ == "__main__":
    main()
