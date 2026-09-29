"""Run the audited daily-open research ledger with blocked-order carry."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from factorlab.audit import (input_fingerprints, load_execution_flags, load_manifest,
                             load_valuation_prices, validate_member_price_coverage,
                             validate_membership, validate_price_availability)
from factorlab.core import build_panel, filter_signals, load_calendar, load_prices
from factorlab.simulator import factor_targets, simulate_open_rebalances


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--calendar-csv", type=Path, required=True)
    parser.add_argument("--membership-csv", type=Path, required=True)
    parser.add_argument("--execution-csv", type=Path, required=True)
    parser.add_argument("--benchmark-csv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--valuation-csv", type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--min-stocks", type=int, default=20)
    parser.add_argument("--min-amount", type=float, default=20_000_000)
    parser.add_argument("--cost-bps", type=float, default=10)
    parser.add_argument("--initial-cash", type=float, default=1_000_000)
    parser.add_argument("--synthetic", action="store_true", help="Label generated fixture data explicitly")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/event_latest"))
    args = parser.parse_args()
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    if start >= end or args.horizon < 1:
        parser.error("Require start < end and positive horizon")
    manifest = load_manifest(args.manifest)
    calendar = load_calendar(args.calendar_csv)
    if start not in calendar or end not in calendar:
        parser.error("Start and end must be trading dates in the supplied calendar")
    signal_dates = calendar[(calendar >= start) & (calendar <= end)]
    last_signal_pos = calendar.get_loc(signal_dates[-1])
    if last_signal_pos + 1 + args.horizon >= len(calendar):
        parser.error("Calendar must cover the final signal's complete holding horizon")
    terminal_day = calendar[last_signal_pos + 1 + args.horizon]
    price_audit = validate_price_availability(args.data_dir, calendar)
    prices = load_prices(args.data_dir, reject_bad_prices=True)
    membership = validate_membership(args.membership_csv, calendar, start, end)
    execution = load_execution_flags(args.execution_csv, calendar)
    valuation = load_valuation_prices(args.valuation_csv, calendar) if args.valuation_csv else None
    missing_members = validate_member_price_coverage(membership, prices, execution, start, end)
    panel = filter_signals(build_panel(prices, args.horizon, calendar), membership, args.min_amount)
    targets = factor_targets(panel, signal_dates, args.horizon, args.min_stocks)
    simulation_calendar = calendar[(calendar >= targets["trade_date"].min()) & (calendar <= terminal_day)]
    benchmark = pd.read_csv(args.benchmark_csv)
    ledger, orders, positions = simulate_open_rebalances(prices, simulation_calendar, targets, execution,
                                                          valuation=valuation, benchmark=benchmark,
                                                          initial_cash=args.initial_cash, cost_bps=args.cost_bps)
    filled = orders.loc[orders["status"] == "filled"] if not orders.empty else orders
    summary = {"engine": "daily_open_fractional_units_v1", "synthetic": args.synthetic,
               "dataset_id": manifest["dataset_id"], "signal_start": args.start,
               "signal_end": args.end, "last_valuation_date": str(ledger.iloc[-1]["date"].date()),
               "signals": len(signal_dates), "rebalance_dates": targets["trade_date"].nunique(),
               "orders_filled": len(filled), "orders_blocked": int(ledger["blocked_orders"].sum()),
               "missing_member_bars_with_nontradable_status": missing_members,
               "initial_cash": args.initial_cash, "final_nav": float(ledger.iloc[-1]["nav"]),
               "total_return": float(ledger.iloc[-1]["return_since_start"]),
               "benchmark_total_return": float(ledger.iloc[-1]["benchmark_return_since_start"]),
               "total_cost": float(ledger["cost"].sum()),
               "production_approved": False,
               "limitations": ["Fractional units; no board-lot rounding or auction queue model",
                               "Costs are a single bps proxy; no stamp-tax schedule or impact curve",
                               "Source manifest and timestamps require independent vendor verification",
                               "No corporate-action cash entitlement and payment ledger"]}
    paths = {"manifest": args.manifest, "calendar": args.calendar_csv,
             "membership": args.membership_csv, "execution": args.execution_csv,
             "benchmark": args.benchmark_csv}
    if args.valuation_csv:
        paths["valuation"] = args.valuation_csv
    audit = input_fingerprints(paths, price_audit)
    code_paths = [Path(__file__), Path(__file__).parents[1] / "factorlab" / "simulator.py",
                  Path(__file__).parents[1] / "factorlab" / "adjustments.py",
                  Path(__file__).parents[1] / "factorlab" / "core.py",
                  Path(__file__).parents[1] / "factorlab" / "audit.py"]
    audit["code_sha256"] = hashlib.sha256(b"".join(path.read_bytes() for path in code_paths)).hexdigest()
    audit["run_id_sha256"] = hashlib.sha256(json.dumps({"inputs": audit, "summary": summary},
                                                 ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    completion = args.output_dir / "complete.json"
    completion.unlink(missing_ok=True)
    for name, frame in (("targets.csv", targets), ("ledger.csv", ledger),
                        ("orders.csv", orders), ("positions.csv", positions)):
        target = args.output_dir / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        frame.to_csv(temporary, index=False)
        temporary.replace(target)
    for name, payload in (("summary.json", summary), ("audit.json", audit)):
        target = args.output_dir / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    products = ("targets.csv", "ledger.csv", "orders.csv", "positions.csv", "summary.json", "audit.json")
    completion_temp = completion.with_suffix(".json.tmp")
    completion_temp.write_text(json.dumps({"run_id_sha256": audit["run_id_sha256"],
                                           "products_sha256": {name: hashlib.sha256(
                                               (args.output_dir / name).read_bytes()).hexdigest() for name in products}},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
    completion_temp.replace(completion)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
