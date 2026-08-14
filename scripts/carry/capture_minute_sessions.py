"""Capture versioned commodity session rules from bounded minute observations."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, time
import os
from pathlib import Path
import tempfile
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from cta_carry.config import CarryConfig
from cta_carry.minute_pg_source import (
    MinuteCandidate,
    PublicMinuteSource,
    minute_contract_identity,
)
from cta_carry.minute_sessions import (
    SESSION_RULES_VERSION,
    SessionRule,
    build_trading_slots,
    load_session_rules,
    resolve_session_rule,
)
from cta_carry.pg_source import load_public_carry_data


SHANGHAI = ZoneInfo("Asia/Shanghai")
CSV_COLUMNS = (
    "exchange",
    "product",
    "effective_start",
    "effective_end",
    "night_end",
    "version",
)
BOUNDARY_COLUMNS = (
    "night_first",
    "night_last",
    "day_1_first",
    "day_1_last",
    "day_2_first",
    "day_2_last",
    "day_3_first",
    "day_3_last",
)
ALLOWED_NIGHT_ENDS = ("none", "23:00", "01:00", "02:30")


class SessionCaptureError(ValueError):
    """Raised when empirical boundaries cannot define one exact session rule."""


@dataclass(frozen=True)
class CapturedCandidate:
    candidate: MinuteCandidate
    previous_trade_date: date


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=SHANGHAI)


def _require_capture_dates(start: date, end: date) -> None:
    if type(start) is not date or type(end) is not date or start > end:
        raise SessionCaptureError("start and end must be ordered concrete dates")


def select_session_candidates(
    prices: pd.DataFrame,
    *,
    start: date,
    end: date,
) -> tuple[CapturedCandidate, ...]:
    """Select one deterministic highest-OI concrete contract per product-day."""
    _require_capture_dates(start, end)
    required = {"trade_date", "product", "contract", "oi", "volume"}
    missing = sorted(required.difference(prices.columns))
    if missing:
        raise SessionCaptureError(f"daily prices missing required columns: {missing}")
    if prices.empty:
        raise SessionCaptureError("daily prices contain no candidate rows")

    frame = prices.loc[:, sorted(required)].copy()
    if any(type(value) is not date for value in frame["trade_date"]):
        raise SessionCaptureError("daily trade_date values must be concrete dates")
    calendar = sorted(set(frame["trade_date"]))
    previous_by_date = {
        current: previous
        for previous, current in zip(calendar, calendar[1:], strict=False)
    }
    frame = frame.loc[frame["trade_date"].between(start, end)].copy()
    if frame.empty:
        raise SessionCaptureError("requested range contains no daily candidates")
    missing_lag = sorted(set(frame["trade_date"]).difference(previous_by_date))
    if missing_lag:
        raise SessionCaptureError(
            "daily history does not contain the previous trading date for "
            + ", ".join(day.isoformat() for day in missing_lag)
        )

    frame = frame.sort_values(
        ["trade_date", "product", "oi", "volume", "contract"],
        ascending=[True, True, False, False, True],
        kind="mergesort",
    ).drop_duplicates(["trade_date", "product"], keep="first")

    selected: list[CapturedCandidate] = []
    for row in frame.itertuples(index=False):
        trade_date = row.trade_date
        product, minute_symbol, exchange = minute_contract_identity(
            row.contract,
            trade_date,
        )
        if type(row.product) is not str or row.product.strip().upper() != product:
            raise SessionCaptureError(
                f"{trade_date} {row.contract}: normalized product identity conflicts"
            )
        previous_trade_date = previous_by_date[trade_date]
        selected.append(
            CapturedCandidate(
                candidate=MinuteCandidate(
                    trade_date=trade_date,
                    product=product,
                    daily_contract=row.contract,
                    minute_symbol=minute_symbol,
                    exchange=exchange,
                    window_start=_at(previous_trade_date, 21, 0),
                    window_end=_at(trade_date, 15, 1),
                ),
                previous_trade_date=previous_trade_date,
            )
        )
    return tuple(selected)


def capture_session_boundaries(
    source: PublicMinuteSource,
    selected: Sequence[CapturedCandidate],
) -> pd.DataFrame:
    """Query grouped boundary observations in target-trade-date calendar months."""
    if not selected:
        raise SessionCaptureError("at least one session candidate is required")
    groups: dict[tuple[int, int], list[CapturedCandidate]] = {}
    for item in selected:
        key = (item.candidate.trade_date.year, item.candidate.trade_date.month)
        groups.setdefault(key, []).append(item)

    frames: list[pd.DataFrame] = []
    for month in sorted(groups):
        batch = groups[month]
        candidates = [item.candidate for item in batch]
        lower = min(item.window_start for item in candidates)
        upper = max(item.window_end for item in candidates)
        frame = source.iter_session_boundaries(candidates, lower=lower, upper=upper)
        lag_by_identity = {
            (
                item.candidate.trade_date,
                item.candidate.product,
                item.candidate.daily_contract,
            ): item.previous_trade_date
            for item in batch
        }
        frame = frame.copy()
        frame["previous_trade_date"] = [
            lag_by_identity[(row.trade_date, row.product, row.daily_contract)]
            for row in frame.itertuples(index=False)
        ]
        frames.append(frame)
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values(["trade_date", "product", "daily_contract"], kind="mergesort")
        .reset_index(drop=True)
    )


def _missing_boundary(value: Any) -> bool:
    return value is None or value is pd.NaT


def _boundary_error(row: Any, field: str, expected: datetime | None) -> None:
    actual = row[field]
    raise SessionCaptureError(
        f"{row['trade_date']} {row['exchange']} {row['product']} "
        f"{row['daily_contract']} {field}: expected {expected!r}; got {actual!r}"
    )


def _require_boundary(row: Any, field: str, expected: datetime) -> None:
    actual = row[field]
    if not isinstance(actual, datetime) or actual.tzinfo is None:
        _boundary_error(row, field, expected)
    try:
        matches = actual.astimezone(SHANGHAI) == expected
    except (TypeError, ValueError, OverflowError):
        matches = False
    if not matches:
        _boundary_error(row, field, expected)


def classify_session_boundary(row: Any) -> str:
    """Classify one exact empirical row into the four supported night clocks."""
    trade_date = row["trade_date"]
    previous_trade_date = row["previous_trade_date"]
    if type(trade_date) is not date or type(previous_trade_date) is not date:
        raise SessionCaptureError("boundary trade dates must be concrete dates")
    expected_day = {
        "day_1_first": _at(trade_date, 9, 0),
        "day_1_last": _at(trade_date, 10, 14),
        "day_2_first": _at(trade_date, 10, 30),
        "day_2_last": _at(trade_date, 11, 29),
        "day_3_first": _at(trade_date, 13, 30),
        "day_3_last": _at(trade_date, 14, 59),
    }
    for field, expected in expected_day.items():
        _require_boundary(row, field, expected)

    night_first = row["night_first"]
    night_last = row["night_last"]
    if _missing_boundary(night_first) and _missing_boundary(night_last):
        return "none"
    if _missing_boundary(night_first) or _missing_boundary(night_last):
        _boundary_error(
            row,
            "night_first" if _missing_boundary(night_first) else "night_last",
            None,
        )
    _require_boundary(row, "night_first", _at(previous_trade_date, 21, 0))
    after_midnight = previous_trade_date.fromordinal(
        previous_trade_date.toordinal() + 1
    )
    endings = {
        "23:00": _at(previous_trade_date, 22, 59),
        "01:00": _at(after_midnight, 0, 59),
        "02:30": _at(after_midnight, 2, 29),
    }
    for label, expected in endings.items():
        try:
            matches = night_last.astimezone(SHANGHAI) == expected
        except (AttributeError, TypeError, ValueError, OverflowError):
            matches = False
        if matches:
            return label
    _boundary_error(row, "night_last", None)
    raise AssertionError("unreachable")


def collapse_session_rules(classified: pd.DataFrame) -> list[dict[str, Any]]:
    """Collapse adjacent audited product dates with identical observed clocks."""
    required = {"exchange", "product", "trade_date", "night_end"}
    missing = sorted(required.difference(classified.columns))
    if missing:
        raise SessionCaptureError(f"classified boundaries missing columns: {missing}")
    ordered = classified.loc[:, sorted(required)].sort_values(
        ["exchange", "product", "trade_date"], kind="mergesort"
    )
    if ordered.duplicated(["exchange", "product", "trade_date"]).any():
        raise SessionCaptureError(
            "classified boundaries contain duplicate product-days"
        )
    if ordered.empty:
        raise SessionCaptureError("no classified boundaries are available")

    rules: list[dict[str, Any]] = []
    for (exchange, product), group in ordered.groupby(
        ["exchange", "product"], sort=True
    ):
        records = list(group.itertuples(index=False))
        start = records[0].trade_date
        night_end = records[0].night_end
        if night_end not in ALLOWED_NIGHT_ENDS:
            raise SessionCaptureError(f"unsupported night_end {night_end!r}")
        previous_date = start
        for record in records[1:]:
            if record.night_end not in ALLOWED_NIGHT_ENDS:
                raise SessionCaptureError(f"unsupported night_end {record.night_end!r}")
            if record.night_end != night_end:
                rules.append(
                    {
                        "exchange": exchange,
                        "product": product,
                        "effective_start": start,
                        "effective_end": previous_date,
                        "night_end": night_end,
                        "version": SESSION_RULES_VERSION,
                    }
                )
                start = record.trade_date
                night_end = record.night_end
            previous_date = record.trade_date
        rules.append(
            {
                "exchange": exchange,
                "product": product,
                "effective_start": start,
                "effective_end": previous_date,
                "night_end": night_end,
                "version": SESSION_RULES_VERSION,
            }
        )
    return sorted(
        rules,
        key=lambda row: (row["exchange"], row["product"], row["effective_start"]),
    )


def _stage_session_rules(output: Path, rules: Sequence[dict[str, Any]]) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
            for rule in rules:
                writer.writerow(
                    {
                        "exchange": rule["exchange"],
                        "product": rule["product"],
                        "effective_start": rule["effective_start"].isoformat(),
                        "effective_end": rule["effective_end"].isoformat(),
                        "night_end": rule["night_end"],
                        "version": rule["version"],
                    }
                )
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _expected_night_end(rule: SessionRule) -> str:
    night_segments = [
        segment
        for segment in rule.segments
        if segment.start_minute < 0 or segment.end_minute <= 150
    ]
    if not night_segments:
        return "none"
    offsets = {
        (-180, -60): "23:00",
        (-180, 60): "01:00",
        (-180, 150): "02:30",
    }
    pair = (night_segments[0].start_minute, night_segments[0].end_minute)
    try:
        return offsets[pair]
    except KeyError as exc:
        raise SessionCaptureError(f"unsupported loaded night segment {pair!r}") from exc


def validate_audited_boundaries(
    boundaries: pd.DataFrame,
    rules: Sequence[SessionRule],
) -> None:
    """Require every empirical boundary to occupy exactly one authoritative slot."""
    for row in boundaries.to_dict("records"):
        rule = resolve_session_rule(
            rules,
            row["exchange"],
            row["product"],
            row["trade_date"],
        )
        actual_night_end = classify_session_boundary(row)
        if _expected_night_end(rule) != actual_night_end:
            raise SessionCaptureError(
                f"{row['trade_date']} {row['product']}: captured rule changed after reload"
            )
        slots = build_trading_slots(
            row["trade_date"],
            row["previous_trade_date"],
            rule,
        )
        for column in BOUNDARY_COLUMNS:
            timestamp = row[column]
            if _missing_boundary(timestamp):
                continue
            if sum(slot == timestamp for slot in slots) != 1:
                raise SessionCaptureError(
                    f"{row['trade_date']} {row['product']} {column}: "
                    "timestamp does not map to exactly one authoritative slot"
                )


def capture_and_publish(
    *,
    start: date,
    end: date,
    output: Path,
    settings: Path | None,
    use_test: bool,
) -> tuple[int, int, int, int]:
    """Capture, validate, and atomically publish the repository rule asset."""
    _require_capture_dates(start, end)
    data = load_public_carry_data(
        start=start,
        end=end,
        config=CarryConfig(prewarm_calendar_days=30),
        config_path=settings,
        use_test=use_test,
    )
    selected = select_session_candidates(data.prices, start=start, end=end)
    source = PublicMinuteSource(config_path=settings, use_test=use_test)
    boundaries = capture_session_boundaries(source, selected)
    products = sorted(set(boundaries["product"]))
    checked_days = len(boundaries)
    classified_rows: list[dict[str, Any]] = []
    ambiguous: list[str] = []
    for row in boundaries.to_dict("records"):
        try:
            night_end = classify_session_boundary(row)
        except SessionCaptureError as exc:
            ambiguous.append(str(exc))
            continue
        classified_rows.append(
            {
                "exchange": row["exchange"],
                "product": row["product"],
                "trade_date": row["trade_date"],
                "night_end": night_end,
            }
        )
    if ambiguous:
        for message in ambiguous:
            print(f"ambiguous_session={message}", file=os.sys.stderr)
        print(
            f"products={len(products)} rules=0 checked_days={checked_days} "
            f"ambiguous={len(ambiguous)}"
        )
        return len(products), 0, checked_days, len(ambiguous)

    rule_rows = collapse_session_rules(pd.DataFrame(classified_rows))
    temporary = _stage_session_rules(output, rule_rows)
    try:
        loaded_rules = load_session_rules(temporary)
        validate_audited_boundaries(boundaries, loaded_rules)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(
        f"products={len(products)} rules={len(loaded_rules)} "
        f"checked_days={checked_days} ambiguous=0"
    )
    return len(products), len(loaded_rules), checked_days, 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture versioned commodity minute-session rules"
    )
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--use-test", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    *_, ambiguous = capture_and_publish(
        start=args.start,
        end=args.end,
        output=args.output,
        settings=args.settings,
        use_test=args.use_test,
    )
    return int(ambiguous != 0)


if __name__ == "__main__":
    raise SystemExit(main())
