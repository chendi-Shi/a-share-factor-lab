"""Generate synthetic CSVs for a smoke test. No market evidence is implied."""

import json
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> None:
    output = Path("data/demo")
    output.mkdir(parents=True, exist_ok=True)
    prices_dir = output / "prices"
    prices_dir.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2022-01-03", periods=900)
    rng = np.random.default_rng(20260928)
    pd.DataFrame({"date": dates}).to_csv(output / "calendar.csv", index=False)
    membership = []
    execution = []
    for index in range(35):
        shocks = rng.normal(0, 0.015, len(dates)) + (index - 17) * 0.000015
        close = 20 * np.exp(np.cumsum(shocks))
        open_ = close * (1 + rng.normal(0, 0.002, len(dates)))
        frame = pd.DataFrame({"date": dates, "symbol": f"{index:06d}",
                              "open": open_, "high": np.maximum(open_, close) * 1.005,
                              "low": np.minimum(open_, close) * 0.995,
                              "close": close, "amount": rng.uniform(30_000_000, 100_000_000, len(dates)),
                              "available_at": [d.strftime("%Y-%m-%dT16:00:00+08:00") for d in dates]})
        frame.to_csv(prices_dir / f"{index:06d}.csv", index=False)
        membership.extend({"date": d, "symbol": f"{index:06d}",
                           "known_at": d.strftime("%Y-%m-%dT08:00:00+08:00")} for d in dates)
        execution.extend({"date": d, "symbol": f"{index:06d}",
                          "can_buy_open": 1, "can_sell_open": 1,
                          "observed_at": d.strftime("%Y-%m-%dT09:30:00+08:00")} for d in dates)
    pd.DataFrame(membership).to_csv(output / "membership.csv", index=False)
    pd.DataFrame(execution).to_csv(output / "execution.csv", index=False)
    pd.DataFrame({"date": dates, "open": 1000 + np.arange(len(dates)) * 0.2}).to_csv(
        output / "benchmark.csv", index=False)
    (output / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "dataset_id": "synthetic-demo-v1", "price_source": "deterministic generator",
        "calendar_source": "pandas business dates", "membership_source": "deterministic generator",
        "benchmark_source": "deterministic generator", "execution_source": "deterministic generator",
        "license": "generated for tests", "price_basis": "point_in_time_adjusted"
    }, indent=2), encoding="utf-8")
    print(f"Generated {35} synthetic symbols in {prices_dir}")


if __name__ == "__main__":
    main()
