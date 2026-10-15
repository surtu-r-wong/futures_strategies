"""Explicit, fail-closed contracts for the supported accounting model."""

from datetime import date, datetime
import hashlib
import json
import math
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
ENGINE_VERSION = "forward-linear-futures-v1"

TSINDEX = {
    "near_leg": "near_dominant",
    "weighting": "rank_linear",
    "carry_window": 90,
    "liquidity_measure": "open_interest_value",
    "liquidity_window": 20,
    "liquidity_threshold": 2e9,
    "stop_loss_enabled": False,
    "trend_filter_enabled": False,
    "missing_open_policy": "defer",
    "target_vol": 0.15,
    "max_gross_leverage": 4.0,
}
SOURCE_RULES = {
    "tsindex_pure": ("cta_carry / tsindex", {**TSINDEX, "basis_momentum_weight": 0.0}),
    "tsindex_bmom020": (
        "cta_carry / tsindex",
        {
            **TSINDEX,
            "basis_momentum_weight": 0.20,
            "basis_momentum_window": 500,
            "basis_momentum_min_coverage": 0.9,
            "basis_momentum_rebalance": "monthly",
        },
    ),
    "citic027_r150_t1": (
        "citic_index / CICSF027.WI",
        {
            "factor_kind": "basis_momentum",
            "window": 150,
            "min_observations": 1,
            "smoothing": 1,
            "t1_leg": "top_two_by_oi",
            "normalise_by_gap": False,
            "return_basis": "close_to_close",
            "execution_lag": 1,
            "cadence": "daily",
            "restrict_to_named": False,
            "roll_blend": True,
            "target_vol": 0.15,
            "vol_window": 252,
            "max_gross_leverage": 4.0,
        },
    ),
}


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def number(value, label, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise ValueError(f"{label}: expected finite number")
    try:
        v = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label}: expected finite number") from exc
    if not math.isfinite(v) or (positive and v <= 0) or (nonnegative and v < 0):
        raise ValueError(f"{label}: invalid numeric value")
    return v


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: nonempty string required")
    return value


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timezone-aware timestamp required") from exc
    if dt.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return dt.astimezone(TZ)


def now_iso():
    return datetime.now(TZ).isoformat()


def day(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ISO trade date required") from exc
    if parsed.isoformat() != value:
        raise ValueError("trade date must use YYYY-MM-DD")
    return parsed


def numeric_field(obj, key, **kwargs):
    if type(obj[key]) not in (int, float):
        raise ValueError(f"{key}: JSON number required")
    return number(obj[key], key, **kwargs)


def require_keys(obj, keys, label, *, exact=True):
    if not isinstance(obj, dict) or not set(keys).issubset(obj):
        raise ValueError(f"{label}: missing required fields")
    if exact and set(obj) != set(keys):
        raise ValueError(f"{label}: unsupported fields")


def validate_config(c, calendar):
    require_keys(
        c,
        (
            "account_id",
            "account_version",
            "adapter",
            "source_version",
            "source_parameters",
            "label",
            "mode",
            "currency",
            "synthetic",
            "initial_at",
            "initial_capital",
            "initial_unit_nav",
            "fee_model" if "fee_model" in c else "cost_bps",
            "fee_description",
            "lot_rule",
            "cash_rule",
            "financing_rule",
            "execution_rule",
            "valuation_rule",
            "calendar_id",
            "calendar_sha256",
            "instruments",
        ),
        "config",
    )
    for key in (
        "account_id",
        "account_version",
        "source_version",
        "label",
        "fee_description",
    ):
        text(c[key], key)
    if c["adapter"] not in SOURCE_RULES:
        raise ValueError("unsupported adapter")
    expected = SOURCE_RULES[c["adapter"]][1]
    if not isinstance(c["source_parameters"], dict) or any(
        c["source_parameters"].get(k) != v for k, v in expected.items()
    ):
        raise ValueError("source parameters do not identify the supported version")
    if c["mode"] != "paper" or c["currency"] != "CNY":
        raise ValueError("only CNY paper linear futures supported")
    if type(c["synthetic"]) is not bool:
        raise ValueError("synthetic must be explicit boolean")
    if c["synthetic"]:
        timestamp(c["initial_at"])
    elif c["initial_at"] is not None:
        raise ValueError(
            "real initialization uses actual time; initial_at must be null"
        )
    numeric_field(c, "initial_capital", positive=True)
    numeric_field(c, "initial_unit_nav", positive=True)
    if "fee_model" in c:
        from .fees import validate_model

        validate_model(c["fee_model"], c["instruments"])
    else:
        numeric_field(c, "cost_bps", nonnegative=True)
    if c["lot_rule"] not in {"fractional", "nearest_integer_ties_even"}:
        raise ValueError("explicit lot_rule required")
    for k, v in {
        "cash_rule": "no_interest_no_external_flows",
        "financing_rule": "no_borrowing",
        "execution_rule": "calendar_day_open",
        "valuation_rule": "daily_settlement",
    }.items():
        if c[k] != v:
            raise ValueError(f"unsupported {k}")
    if not isinstance(c["instruments"], dict) or not c["instruments"]:
        raise ValueError("explicit contract specifications required")
    for contract, spec in c["instruments"].items():
        text(contract, "contract")
        require_keys(
            spec,
            (
                "asset_type",
                "currency",
                "multiplier",
                "margin_rate",
                "valid_from",
                "valid_to",
                "source",
            ),
            "instrument",
        )
        if spec["asset_type"] != "linear_futures" or spec["currency"] != "CNY":
            raise ValueError("only CNY linear futures instruments supported")
        numeric_field(spec, "multiplier", positive=True)
        rate = numeric_field(spec, "margin_rate", positive=True)
        if rate > 1:
            raise ValueError("margin_rate must be <= 1")
        if day(spec["valid_from"]) > day(spec["valid_to"]):
            raise ValueError("invalid instrument validity")
        text(spec["source"], "instrument source")
    validate_calendar(calendar)
    if c["calendar_id"] != calendar["calendar_id"] or c["calendar_sha256"] != digest(
        calendar
    ):
        raise ValueError("calendar identity/hash mismatch")
    canonical(c)


def validate_calendar(cal):
    require_keys(
        cal,
        (
            "calendar_id",
            "timezone",
            "source",
            "complete",
            "coverage_start",
            "coverage_end",
            "intervals",
        ),
        "calendar",
    )
    text(cal["calendar_id"], "calendar_id")
    text(cal["source"], "calendar source")
    if cal["complete"] is not True or cal["timezone"] != "Asia/Shanghai":
        raise ValueError("complete Asia/Shanghai calendar declaration required")
    if not isinstance(cal["intervals"], list) or not cal["intervals"]:
        raise ValueError("calendar intervals required")
    previous = None
    for row in cal["intervals"]:
        require_keys(
            row,
            ("trade_date", "signal_date", "start_at", "execution_at", "end_at"),
            "interval",
        )
        d = day(row["trade_date"])
        sig = day(row["signal_date"])
        start, execution, end = [
            timestamp(row[k]) for k in ("start_at", "execution_at", "end_at")
        ]
        if (
            not start < execution < end
            or execution.date() != d
            or end.date() != d
            or sig >= d
        ):
            raise ValueError("invalid daily execution/settlement interval")
        if execution.hour < 8 or execution.hour >= 12:
            raise ValueError("only explicitly mapped day-open executions supported")
        if previous and (
            start != timestamp(previous["end_at"])
            or row["signal_date"] != previous["trade_date"]
        ):
            raise ValueError(
                "calendar intervals must be contiguous with prior signal day"
            )
        previous = row
    if (
        cal["coverage_start"] != cal["intervals"][0]["trade_date"]
        or cal["coverage_end"] != previous["trade_date"]
    ):
        raise ValueError("calendar coverage does not match intervals")
