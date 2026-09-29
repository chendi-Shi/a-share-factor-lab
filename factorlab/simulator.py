"""Daily-open research ledger with cash, blocked orders, and carried positions.

Units are fractional research units. Real board lots, corporate-action cash
payments, auction queue position, and market impact require separate data.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .core import FACTOR_COLUMNS


def factor_targets(panel: pd.DataFrame, signal_dates: pd.DatetimeIndex,
                   horizon: int = 5, min_stocks: int = 20) -> pd.DataFrame:
    """Freeze target weights from signals; never inspect future returns."""
    if horizon < 1 or min_stocks < 1:
        raise ValueError("horizon and min_stocks must be positive")
    rows = []
    for signal_date in signal_dates[::horizon]:
        group = panel.loc[panel["date"] == signal_date].dropna(subset=list(FACTOR_COLUMNS)).copy()
        if len(group) < min_stocks:
            raise ValueError(f"Only {len(group)} eligible stocks on {signal_date.date()}")
        if group["symbol"].duplicated().any() or group["entry_date"].nunique() != 1:
            raise ValueError(f"Ambiguous target inputs on {signal_date.date()}")
        for factor in FACTOR_COLUMNS:
            group[f"rank_{factor}"] = group[factor].rank(pct=True, method="average")
        group["score"] = group[[f"rank_{factor}" for factor in FACTOR_COLUMNS]].mean(axis=1)
        group = group.sort_values(["score", "symbol"], ascending=[False, True])
        top = group.head(max(1, math.ceil(len(group) * 0.2)))
        trade_date = top["entry_date"].iloc[0]
        if pd.isna(trade_date):
            raise ValueError(f"No trading date after signal {signal_date.date()}")
        rows.extend({"signal_date": signal_date, "trade_date": trade_date,
                     "symbol": symbol, "target_weight": 1 / len(top)}
                    for symbol in top["symbol"])
    return pd.DataFrame(rows, columns=["signal_date", "trade_date", "symbol", "target_weight"])


def simulate_open_rebalances(prices: pd.DataFrame, calendar: pd.DatetimeIndex,
                             targets: pd.DataFrame, execution: pd.DataFrame,
                             *, valuation: pd.DataFrame | None = None,
                             benchmark: pd.DataFrame | None = None,
                             initial_cash: float = 1_000_000,
                             cost_bps: float = 10) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Sell first, then buy pro rata with available cash; carry blocked orders.

    A missing open for a held stock needs an explicit daily valuation. A missing
    open for a new buy may be skipped only if the execution flag blocks buying.
    """
    if not np.isfinite(initial_cash) or initial_cash <= 0 or not np.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("initial_cash must be positive and cost_bps nonnegative")
    if not {"date", "symbol", "open"}.issubset(prices.columns):
        raise ValueError("Prices require date,symbol,open")
    if not {"trade_date", "symbol", "target_weight"}.issubset(targets.columns):
        raise ValueError("Targets require trade_date,symbol,target_weight")
    if not {"date", "symbol", "can_buy_open", "can_sell_open"}.issubset(execution.columns):
        raise ValueError("Execution requires date,symbol,can_buy_open,can_sell_open")
    bars = prices.copy()
    bars["date"] = pd.to_datetime(bars["date"], errors="raise")
    bars["symbol"] = bars["symbol"].astype(str).str.zfill(6)
    bars["open"] = pd.to_numeric(bars["open"], errors="coerce")
    if bars.duplicated(["date", "symbol"]).any():
        raise ValueError("Duplicate daily open")
    opens = bars.set_index(["date", "symbol"])["open"]
    planned = targets.copy()
    planned["trade_date"] = pd.to_datetime(planned["trade_date"], errors="raise")
    planned["symbol"] = planned["symbol"].astype(str).str.zfill(6)
    planned["target_weight"] = pd.to_numeric(planned["target_weight"], errors="coerce")
    if planned.duplicated(["trade_date", "symbol"]).any() or not planned["trade_date"].isin(calendar).all():
        raise ValueError("Duplicate or out-of-calendar targets")
    if not np.isfinite(planned["target_weight"]).all() or planned["target_weight"].lt(0).any():
        raise ValueError("Target weights must be finite and nonnegative")
    if planned.groupby("trade_date")["target_weight"].sum().gt(1 + 1e-9).any():
        raise ValueError("Target weights exceed 100%")
    flags = execution.copy()
    flags["date"] = pd.to_datetime(flags["date"], errors="raise")
    flags["symbol"] = flags["symbol"].astype(str).str.zfill(6)
    if flags.duplicated(["date", "symbol"]).any():
        raise ValueError("Duplicate execution flags")
    if not flags[["can_buy_open", "can_sell_open"]].isin([0, 1, True, False]).all().all():
        raise ValueError("Execution flags must be boolean")
    flags = flags.set_index(["date", "symbol"])
    marks = None
    if valuation is not None:
        if not {"date", "symbol", "price"}.issubset(valuation.columns):
            raise ValueError("Valuation requires date,symbol,price")
        values = valuation.copy()
        values["date"] = pd.to_datetime(values["date"], errors="raise")
        values["symbol"] = values["symbol"].astype(str).str.zfill(6)
        values["price"] = pd.to_numeric(values["price"], errors="coerce")
        if values.duplicated(["date", "symbol"]).any():
            raise ValueError("Duplicate valuation price")
        marks = values.set_index(["date", "symbol"])["price"]
    index_open = None
    if benchmark is not None:
        index = benchmark.copy()
        index["date"] = pd.to_datetime(index["date"], errors="raise")
        if index["date"].duplicated().any():
            raise ValueError("Duplicate benchmark date")
        index_open = pd.to_numeric(index.set_index("date")["open"], errors="coerce")
    target_by_date = {day: dict(zip(group["symbol"], group["target_weight"]))
                      for day, group in planned.groupby("trade_date")}
    if not target_by_date:
        raise ValueError("No target rebalance dates")
    first = min(target_by_date)
    days = calendar[calendar >= first]
    if len(days) == 0:
        raise ValueError("No calendar dates after first rebalance")
    cash = float(initial_cash)
    holdings: dict[str, float] = {}
    ledger = []
    orders = []
    positions = []
    benchmark_base = None

    def mark_price(day, symbol):
        key = (day, symbol)
        price = opens.get(key, np.nan)
        if not np.isfinite(price) or price <= 0:
            price = marks.get(key, np.nan) if marks is not None else np.nan
        if not np.isfinite(price) or price <= 0:
            raise ValueError(f"Missing open/valuation for held {symbol} on {day.date()}")
        return float(price)

    def trade_flag(day, symbol, direction):
        key = (day, symbol)
        if key not in flags.index:
            raise ValueError(f"Missing execution flag for {symbol} on {day.date()}")
        return bool(flags.loc[key, f"can_{direction}_open"])

    for day in days:
        held_prices = {symbol: mark_price(day, symbol) for symbol in holdings}
        nav_before = cash + sum(quantity * held_prices[symbol] for symbol, quantity in holdings.items())
        if not np.isfinite(nav_before) or nav_before <= 0:
            raise ValueError(f"Invalid NAV on {day.date()}")
        traded_notional = 0.0
        fees = 0.0
        blocked = 0
        if day in target_by_date:
            desired = target_by_date[day]
            # Sells happen first. A blocked sell remains a marked holding.
            for symbol in sorted(holdings):
                price = held_prices[symbol]
                target_qty = nav_before * desired.get(symbol, 0.0) / price
                sell_qty = max(0.0, holdings[symbol] - target_qty)
                if sell_qty <= 1e-12:
                    continue
                if not trade_flag(day, symbol, "sell"):
                    blocked += 1
                    orders.append({"date": day, "symbol": symbol, "side": "sell", "status": "blocked",
                                   "quantity": sell_qty, "notional": 0.0, "cost": 0.0})
                    continue
                if not np.isfinite(opens.get((day, symbol), np.nan)) or opens.get((day, symbol), np.nan) <= 0:
                    raise ValueError(f"Cannot fill sell without an open for {symbol} on {day.date()}")
                notional = sell_qty * price
                cost = notional * cost_bps / 10_000
                holdings[symbol] -= sell_qty
                if holdings[symbol] <= 1e-12:
                    del holdings[symbol]
                cash += notional - cost
                traded_notional += notional
                fees += cost
                orders.append({"date": day, "symbol": symbol, "side": "sell", "status": "filled",
                               "quantity": sell_qty, "notional": notional, "cost": cost})
            # Reserve one common cash scaling factor to avoid symbol-order bias.
            requests = []
            for symbol, weight in sorted(desired.items()):
                key = (day, symbol)
                price = opens.get(key, np.nan)
                current_qty = holdings.get(symbol, 0.0)
                if not np.isfinite(price) or price <= 0:
                    if current_qty > 0:
                        target_qty = nav_before * weight / held_prices[symbol]
                        if target_qty <= current_qty + 1e-12:
                            continue
                        unfilled_qty = target_qty - current_qty
                    else:
                        unfilled_qty = np.nan
                    if not trade_flag(day, symbol, "buy"):
                        blocked += 1
                        orders.append({"date": day, "symbol": symbol, "side": "buy", "status": "blocked",
                                       "quantity": unfilled_qty, "notional": 0.0, "cost": 0.0})
                        continue
                    raise ValueError(f"Missing open for buyable {symbol} on {day.date()}")
                buy_qty = max(0.0, nav_before * weight / price - current_qty)
                if buy_qty <= 1e-12:
                    continue
                if not trade_flag(day, symbol, "buy"):
                    blocked += 1
                    orders.append({"date": day, "symbol": symbol, "side": "buy", "status": "blocked",
                                   "quantity": buy_qty, "notional": 0.0, "cost": 0.0})
                    continue
                requests.append((symbol, price, buy_qty))
            required_cash = sum(price * quantity * (1 + cost_bps / 10_000)
                                for _, price, quantity in requests)
            scale = min(1.0, cash / required_cash) if required_cash else 1.0
            for symbol, price, quantity in requests:
                buy_qty = quantity * scale
                if buy_qty <= 1e-12:
                    continue
                notional = buy_qty * price
                cost = notional * cost_bps / 10_000
                holdings[symbol] = holdings.get(symbol, 0.0) + buy_qty
                cash -= notional + cost
                traded_notional += notional
                fees += cost
                orders.append({"date": day, "symbol": symbol, "side": "buy", "status": "filled",
                               "quantity": buy_qty, "notional": notional, "cost": cost})
        if cash < -1e-6:
            raise ValueError(f"Negative cash on {day.date()}")
        if abs(cash) < 1e-6:
            cash = 0.0
        nav_after = cash + sum(quantity * (held_prices[symbol] if symbol in held_prices
                                           else mark_price(day, symbol))
                               for symbol, quantity in holdings.items())
        benchmark_return = np.nan
        if index_open is not None:
            index_price = index_open.get(day, np.nan)
            if not np.isfinite(index_price) or index_price <= 0:
                raise ValueError(f"Missing benchmark open on {day.date()}")
            if benchmark_base is None:
                benchmark_base = float(index_price)
            benchmark_return = float(index_price / benchmark_base - 1)
        ledger.append({"date": day, "cash": cash, "holdings_n": len(holdings),
                       "nav": nav_after, "return_since_start": nav_after / initial_cash - 1,
                       "benchmark_return_since_start": benchmark_return,
                       "traded_notional": traded_notional, "cost": fees,
                       "blocked_orders": blocked})
        for symbol, quantity in sorted(holdings.items()):
            price = held_prices[symbol] if symbol in held_prices else mark_price(day, symbol)
            source = "open" if np.isfinite(opens.get((day, symbol), np.nan)) and \
                opens.get((day, symbol), np.nan) > 0 else "valuation"
            positions.append({"date": day, "symbol": symbol, "quantity": quantity,
                              "mark_price": price, "mark_source": source,
                              "market_value": quantity * price})
    return pd.DataFrame(ledger), pd.DataFrame(orders), pd.DataFrame(positions)
