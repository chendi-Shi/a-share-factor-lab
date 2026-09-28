"""Generate synthetic CSVs for a smoke test. No market evidence is implied."""

from pathlib import Path

import numpy as np
import pandas as pd


def main() -> None:
    output = Path("data/demo")
    output.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2022-01-03", periods=900)
    rng = np.random.default_rng(20260928)
    for index in range(35):
        shocks = rng.normal(0, 0.015, len(dates)) + (index - 17) * 0.000015
        close = 20 * np.exp(np.cumsum(shocks))
        open_ = close * (1 + rng.normal(0, 0.002, len(dates)))
        frame = pd.DataFrame({"date": dates, "symbol": f"{index:06d}",
                              "open": open_, "high": np.maximum(open_, close) * 1.005,
                              "low": np.minimum(open_, close) * 0.995,
                              "close": close, "amount": rng.uniform(30_000_000, 100_000_000, len(dates))})
        frame.to_csv(output / f"{index:06d}.csv", index=False)
    print(f"Generated {35} synthetic symbols in {output}")


if __name__ == "__main__":
    main()
