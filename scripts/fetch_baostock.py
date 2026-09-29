"""Fetch a local BaoStock research snapshot with unverified redistribution rights.

The resulting files are intentionally *not* strict-mode inputs. BaoStock does not
provide observed publication times, adjustment vintages, or open-auction fills.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path


PRICE_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,isST"
INDEX_FIELDS = "date,code,open,high,low,close,volume,amount"


def collect(result, label: str) -> list[dict[str, str]]:
    if result.error_code != "0":
        raise RuntimeError(f"{label}: {result.error_code} {result.error_msg}")
    records = []
    while result.next():
        records.append(dict(zip(result.fields, result.get_row_data(), strict=True)))
    if result.error_code != "0":
        raise RuntimeError(f"{label}: {result.error_code} {result.error_msg}")
    return records


def write_csv(path: Path, fields: list[str], records: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    temporary.replace(path)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def six_digit(code: str) -> str:
    exchange, separator, number = code.partition(".")
    if exchange not in {"sh", "sz"} or separator != "." or len(number) != 6 or not number.isdigit():
        raise ValueError(f"Unexpected BaoStock A-share code: {code}")
    return number


def validate_members(date_text: str, records: list[dict[str, str]]) -> list[dict[str, str]]:
    if len(records) != 500:
        raise ValueError(f"{date_text}: expected 500 CSI 500 members; got {len(records)}")
    codes = [row["code"] for row in records]
    if len(set(codes)) != 500:
        raise ValueError(f"{date_text}: duplicate constituent codes")
    for row in records:
        six_digit(row["code"])
        if row["updateDate"] > date_text:
            raise ValueError(f"{date_text}: source update date is in the future")
    return records


def validate_bars(code: str, records: list[dict[str, str]]) -> list[dict[str, str]]:
    dates = [row["date"] for row in records]
    if dates != sorted(set(dates)) or any(row["code"] != code for row in records):
        raise ValueError(f"{code}: unsorted, duplicate or mismatched bars")
    return records


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="First membership trading date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Last membership trading date, YYYY-MM-DD")
    parser.add_argument("--output-dir", type=Path, default=Path("data/baostock_csi500"))
    parser.add_argument("--lookback-days", type=int, default=120,
                        help="Calendar days of earlier price history for factor warmup")
    parser.add_argument("--symbols", help="Comma-separated BaoStock codes for a partial price pilot")
    parser.add_argument("--pause", type=float, default=0.2, help="Seconds between server requests")
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if start > end or end > date.today() or args.lookback_days < 0 or args.pause < 0:
        parser.error("Require start <= end <= today, nonnegative lookback-days and pause")
    requested = [item.strip() for item in args.symbols.split(",")] if args.symbols else None
    if requested is not None:
        if not requested or len(set(requested)) != len(requested):
            parser.error("--symbols must contain unique BaoStock codes")
        for code in requested:
            six_digit(code)

    try:
        import baostock as bs
    except ImportError as exc:
        parser.error("Install optional dependency: python -m pip install baostock==0.9.4")
        raise AssertionError from exc

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    request = {"schema_version": 2, "start": args.start, "end": args.end, "lookback_days": args.lookback_days,
               "symbols": requested}
    request_path = output / "request.json"
    if request_path.exists() and json.loads(request_path.read_text(encoding="utf-8")) != request:
        raise ValueError(f"{output} contains a different request; use another --output-dir")
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata_path = output / "source_metadata.json"
    metadata_path.unlink(missing_ok=True)
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_code} {login.error_msg}")
    requests_made = 0

    def query(function, label: str, *positional, **keywords):
        nonlocal requests_made
        if requests_made:
            time.sleep(args.pause)
        result = collect(function(*positional, **keywords), label)
        requests_made += 1
        return result

    try:
        calendar_raw = query(bs.query_trade_dates, "trade calendar", start_date=args.start, end_date=args.end)
        trading_dates = [row["calendar_date"] for row in calendar_raw if row["is_trading_day"] == "1"]
        if not trading_dates or trading_dates != sorted(set(trading_dates)):
            raise ValueError("BaoStock calendar is empty, unsorted or duplicated")
        write_csv(output / "calendar.csv", ["date"], [{"date": day} for day in trading_dates])

        membership = []
        observations = []
        for index, day in enumerate(trading_dates, 1):
            cache = output / "membership_daily" / f"{day}.csv"
            if cache.exists():
                rows = validate_members(day, read_csv(cache))
            else:
                rows = validate_members(day, query(bs.query_zz500_stocks, f"members {day}", day))
                write_csv(cache, ["updateDate", "code"], rows)
            membership.extend({"date": day, "symbol": six_digit(row["code"])} for row in rows)
            observations.append({"date": day, "source_update_dates": ";".join(sorted({r["updateDate"] for r in rows})),
                                 "member_count": "500", "retrieved_at_utc":
                                 datetime.fromtimestamp(cache.stat().st_mtime, tz=timezone.utc).isoformat(),
                                 "sha256": checksum(cache)})
            print(f"membership {index}/{len(trading_dates)} {day}", flush=True)
        write_csv(output / "membership.csv", ["date", "symbol"], membership)
        write_csv(output / "membership_observations.csv",
                  ["date", "source_update_dates", "member_count", "retrieved_at_utc", "sha256"], observations)

        universe = sorted({row["symbol"] for row in membership})
        source_codes = {six_digit(row["code"]): row["code"] for day in trading_dates
                        for row in read_csv(output / "membership_daily" / f"{day}.csv")}
        if len(source_codes) != len({row["code"] for day in trading_dates
                                     for row in read_csv(output / "membership_daily" / f"{day}.csv")}):
            raise ValueError("Different exchange codes collapse to the same six-digit symbol")
        if requested is not None:
            missing = sorted(set(requested) - set(source_codes.values()))
            if missing:
                raise ValueError(f"Requested codes are absent from historical membership: {missing}")
            codes = requested
        else:
            codes = [source_codes[symbol] for symbol in universe]
        price_start = (start - timedelta(days=args.lookback_days)).isoformat()
        basic_rows = []
        for index, code in enumerate(codes, 1):
            symbol = six_digit(code)
            price_path = output / "prices_raw" / f"{symbol}.csv"
            if not price_path.exists():
                bars = validate_bars(code, query(bs.query_history_k_data_plus, f"prices {code}", code,
                                                 PRICE_FIELDS, start_date=price_start, end_date=args.end,
                                                 frequency="d", adjustflag="3"))
                if not bars:
                    raise ValueError(f"{code}: no raw bars in requested range")
                write_csv(price_path, ["date", "symbol", "open", "high", "low", "close", "preclose", "amount",
                                       "volume", "tradestatus", "isST", "adjustflag"],
                          [{**bar, "symbol": symbol} for bar in bars])
            adjust_path = output / "adjust_factors" / f"{symbol}.csv"
            if not adjust_path.exists():
                adjustments = query(bs.query_adjust_factor, f"adjust factors {code}", code,
                                    start_date=price_start, end_date=args.end)
                write_csv(adjust_path, ["code", "dividOperateDate", "foreAdjustFactor",
                                        "backAdjustFactor", "adjustFactor"], adjustments)
            basic = query(bs.query_stock_basic, f"stock basic {code}", code=code)
            if len(basic) != 1 or basic[0]["code"] != code:
                raise ValueError(f"{code}: missing or ambiguous stock basic record")
            basic_rows.append({key: basic[0][key] for key in ("code", "ipoDate", "outDate", "type", "status")})
            print(f"symbol {index}/{len(codes)} {code}", flush=True)
        write_csv(output / "stock_basic.csv", ["code", "ipoDate", "outDate", "type", "status"], basic_rows)

        benchmark = validate_bars("sh.000905", query(bs.query_history_k_data_plus, "CSI 500 index",
                                                    "sh.000905", INDEX_FIELDS, start_date=price_start,
                                                    end_date=args.end, frequency="d", adjustflag="3"))
        if not benchmark:
            raise ValueError("CSI 500 benchmark has no bars")
        missing_benchmark = sorted(set(trading_dates) - {row["date"] for row in benchmark})
        if missing_benchmark:
            raise ValueError(f"CSI 500 benchmark misses trading dates: {missing_benchmark[:5]}")
        write_csv(output / "benchmark.csv", ["date", "open", "high", "low", "close", "amount"], benchmark)
        metadata = {
            "source": "BaoStock", "source_url": "https://www.baostock.com/",
            "package_version": version("baostock"),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "membership_start": args.start, "membership_end": args.end,
            "price_start": price_start, "price_end": args.end,
            "trading_days": len(trading_dates), "historical_universe_symbols": len(universe),
            "downloaded_price_symbols": len(codes), "price_sample_only": len(codes) != len(universe),
            "price_basis": "unadjusted", "raw_bar_schema_version": 2,
            "data_redistribution_rights": "not verified",
            "strict_mode_eligible": False,
            "limitations": ["No verified historical publication/known_at timestamps",
                            "No point-in-time adjustment vintages",
                            "No open-auction limit or fillability flags",
                            "Raw prices jump across corporate actions"],
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
    finally:
        bs.logout()


if __name__ == "__main__":
    main()
