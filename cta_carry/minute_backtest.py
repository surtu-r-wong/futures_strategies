"""Intraday stop decisions and close-time target merging for Carry."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
import hashlib
import json
import math
import re
from typing import Any

from zoneinfo import ZoneInfo

import pandas as pd

from .backtest import (
    CarryBacktestResult,
    WarmupInsufficientError,
    _contract_products,
    _position_rows,
    _records_frame,
    _summary_metrics,
)
from .config import CarryConfig
from .data import CarryDataSet
from .decision import TargetPlan, plan_signal_targets
from .decision import DailyResearch, build_daily_research
from .minute_account import AccountEvent, EventAccount
from .minute_bars import (
    FifteenMinuteBar,
    MinuteDataError,
    MultiplierResolution,
    VwapFill,
    aggregate_fifteen_minute_bar,
    five_minute_vwap,
)
from .minute_pg_source import MinuteCandidate, minute_contract_identity
from .minute_sessions import (
    SESSION_RULES_CAPTURE_START,
    SESSION_RULES_VERSION,
    SessionClockError,
    SessionRule,
    build_trading_slots,
    fifteen_minute_buckets,
    next_slots,
    resolve_session_rule,
    validate_capture_coverage,
)
from .risk import PositionState, ShadowVolWindow, apply_chandelier, scale_weights


@dataclass(frozen=True)
class StopDecision:
    """The auditable result of checking one complete 15-minute bar."""

    eligible: bool
    triggered: bool
    state: PositionState
    stage: int | None
    threshold: float | None
    bar: FifteenMinuteBar
    not_before: datetime | None


def _is_timezone_aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _validate_bar_and_fill_timing(
    bar: FifteenMinuteBar,
    next_fill_end: datetime,
) -> None:
    if not _is_timezone_aware(bar.start) or not _is_timezone_aware(bar.end):
        raise ValueError("bar.start and bar.end must be timezone-aware")
    if not _is_timezone_aware(next_fill_end):
        raise ValueError("next_fill_end must be timezone-aware")
    try:
        if bar.start >= bar.end:
            raise ValueError("bar.end must be after bar.start")
        if next_fill_end <= bar.end:
            raise ValueError("next_fill_end must be after bar.end")
    except TypeError as exc:
        raise ValueError(
            "bar.start, bar.end, and next_fill_end must be comparable"
        ) from exc


class IntradayStopMachine:
    """Apply at most one stop tranche per eligible 15-minute bar."""

    def __init__(self, config: CarryConfig) -> None:
        self.config = config
        self._not_before_by_product: dict[str, datetime] = {}
        self._trigger_counts: dict[tuple[str, date], int] = {}

    def not_before(self, product: str) -> datetime | None:
        return self._not_before_by_product.get(product)

    def reset_for_transition(
        self,
        product: str,
        before: PositionState,
        after: PositionState,
        *,
        fill_end: datetime | None,
    ) -> None:
        """Reset the eligibility gate after an exit or a new execution leg."""
        before_direction = before.direction or before.locked_direction
        after_direction = after.direction or after.locked_direction
        exited = before_direction != 0 and after_direction == 0
        entered = before_direction == 0 and after.direction != 0
        reversed_direction = (
            before_direction != 0 and after.direction == -before_direction
        )
        rolled = (
            before.direction == after.direction != 0
            and before.contract != after.contract
        )

        if exited:
            self._not_before_by_product.pop(product, None)
        elif entered or reversed_direction or rolled:
            if fill_end is None:
                raise ValueError("fill_end is required for entry, reversal, or roll")
            if not _is_timezone_aware(fill_end):
                raise ValueError("fill_end must be timezone-aware")
            self._not_before_by_product[product] = fill_end

    def on_bar(
        self,
        trade_date: date,
        product: str,
        state: PositionState,
        bar: FifteenMinuteBar,
        atr: float,
        next_fill_end: datetime,
    ) -> StopDecision:
        _validate_bar_and_fill_timing(bar, next_fill_end)
        if state.direction != 0 and bar.contract != state.contract:
            raise ValueError(
                "bar.contract must equal state.contract for an active state"
            )

        gate = self._not_before_by_product.get(product)
        count_key = (product, trade_date)
        stopped_out = state.direction == 0
        before_gate = gate is not None and bar.start < gate
        limit_reached = (
            self._trigger_counts.get(count_key, 0) >= self.config.stop_tranches
        )
        if bar.no_trade or stopped_out or before_gate or limit_reached:
            return StopDecision(
                eligible=False,
                triggered=False,
                state=state,
                stage=None,
                threshold=None,
                bar=bar,
                not_before=gate,
            )

        updated, triggered = apply_chandelier(
            state,
            bar.high,
            bar.low,
            bar.close,
            atr,
            self.config,
        )
        if state.direction == 1:
            extreme = (
                bar.high
                if state.highest_high is None
                else max(state.highest_high, bar.high)
            )
            threshold = extreme - self.config.chandelier_atr_multiple * atr
        else:
            extreme = (
                bar.low if state.lowest_low is None else min(state.lowest_low, bar.low)
            )
            threshold = extreme + self.config.chandelier_atr_multiple * atr

        stage = None
        if triggered:
            self._trigger_counts[count_key] = self._trigger_counts.get(count_key, 0) + 1
            self._not_before_by_product[product] = next_fill_end
            gate = next_fill_end
            stage = self.config.stop_tranches - updated.tranches_remaining

        return StopDecision(
            eligible=True,
            triggered=triggered,
            state=updated,
            stage=stage,
            threshold=threshold,
            bar=bar,
            not_before=gate,
        )


def _validated_final_stop_stages(
    post_stop_states: Mapping[str, PositionState],
    final_stop_decisions: Mapping[str, StopDecision],
    config: CarryConfig,
) -> dict[str, int]:
    stages: dict[str, int] = {}
    for product, decision in sorted(final_stop_decisions.items()):
        if product not in post_stop_states:
            raise ValueError(
                f"final_stop_decisions contains unknown product: {product}"
            )
        if not isinstance(decision, StopDecision):
            raise ValueError(f"final_stop_decisions[{product}] must be a StopDecision")
        state = post_stop_states[product]
        if decision.state != state:
            raise ValueError(
                f"final_stop_decisions[{product}].state does not match post_stop_states"
            )
        if not decision.triggered:
            if decision.stage is not None:
                raise ValueError(
                    f"final_stop_decisions[{product}] has a stage without a trigger"
                )
            continue
        expected_stage = config.stop_tranches - state.tranches_remaining
        if (
            not decision.eligible
            or decision.stage != expected_stage
            or not 1 <= expected_stage <= config.stop_tranches
        ):
            raise ValueError(
                f"final_stop_decisions[{product}] has inconsistent trigger stage"
            )
        if state.direction != 0 and decision.bar.contract != state.contract:
            raise ValueError(
                f"final_stop_decisions[{product}].bar contract does not match state"
            )
        stages[product] = expected_stage
    return stages


def merge_close_plan(
    post_stop_states: dict[str, PositionState],
    signal_rows: pd.DataFrame,
    config: CarryConfig,
    *,
    final_stop_decisions: Mapping[str, StopDecision] | None = None,
) -> TargetPlan:
    """Merge a final-bar stop and the close signal into one net target plan."""
    duplicate_mask = signal_rows["product"].duplicated(keep=False)
    if duplicate_mask.any():
        duplicate_products = sorted(
            str(product)
            for product in signal_rows.loc[duplicate_mask, "product"].unique()
        )
        raise ValueError(
            f"signal_rows contains duplicate product rows: {duplicate_products}"
        )

    final_stop_stages = _validated_final_stop_stages(
        post_stop_states,
        final_stop_decisions or {},
        config,
    )
    signals = {
        row.product: row
        for row in signal_rows.sort_values("product", kind="mergesort").itertuples(
            index=False
        )
    }
    reason_hints: dict[str, str] = {}
    for product, state in post_stop_states.items():
        signal = signals.get(product)
        if signal is None:
            if state.locked_direction != 0:
                reason_hints[product] = "signal_exit"
            continue
        signal_direction = int(signal.effective_direction)
        if signal_direction == 0 and state.locked_direction != 0:
            reason_hints[product] = "signal_exit"
            continue

        stage = final_stop_stages.get(product)
        if stage is None:
            continue
        if state.direction != 0:
            same_cycle = (
                signal_direction == state.direction
                and signal.main_contract == state.contract
            )
        else:
            same_cycle = (
                state.locked_direction != 0
                and signal_direction == state.locked_direction
            )
        if same_cycle:
            reason_hints[product] = f"stop_{stage}"

    return plan_signal_targets(
        post_stop_states,
        signal_rows,
        config,
        previous_states=post_stop_states,
        reason_hints=reason_hints,
    )


SHANGHAI = ZoneInfo("Asia/Shanghai")
MINUTE_QUERY_RULES_VERSION = "timescale-bare-symbol-v1"
MULTIPLIER_RESOLUTION_VERSION = "price-range-v1"

_CANDIDATE_ROLES = frozenset(
    {"signal_main", "carried", "roll_old", "roll_new", "exit", "close_mark"}
)
_ROLE_PRIORITY = {
    "roll_old": 0,
    "roll_new": 1,
    "exit": 2,
    "carried": 3,
    "signal_main": 4,
    "close_mark": 5,
}
_DAILY_COLUMNS = (
    "trade_date",
    "gross_return",
    "turnover",
    "cost",
    "net_return",
    "equity",
    "gross_leverage",
    "boundary_type",
)
_POSITION_COLUMNS = (
    "trade_date",
    "product",
    "contract",
    "direction",
    "raw_weight",
    "weight",
    "gross_leverage",
    "tranches_remaining",
    "highest_high",
    "lowest_low",
    "locked_direction",
    "carried_in",
)
_TRADE_COLUMNS = (
    "trade_date",
    "product",
    "contract",
    "old_weight",
    "new_weight",
    "weight_change",
    "reason",
)
_EXECUTION_COLUMNS = (
    "execution_id",
    "trade_date",
    "product",
    "contract",
    "candidate_role",
    "reason",
    "execution_kind",
    "signal_time",
    "window_start",
    "window_end",
    "vwap",
    "daily_open",
    "old_weight",
    "new_weight",
    "weight_change",
    "turnover",
    "cost",
    "volume",
    "amount",
    "missing_slots",
    "multiplier",
    "multiplier_source",
    "multiplier_pass_rate",
    "multiplier_sample_rows",
)
_STOP_COLUMNS = (
    "trade_date",
    "product",
    "contract",
    "bar_start",
    "bar_end",
    "open",
    "high",
    "low",
    "close",
    "atr",
    "threshold",
    "tranches_before",
    "tranches_after",
    "locked_direction",
    "triggered",
    "execution_id",
)
_QUALITY_COLUMNS = (
    "check",
    "trade_date",
    "product",
    "contract",
    "candidate_role",
    "observed_rows",
    "missing_slots",
    "query_month",
    "detail",
)
_RUN_CONFIG_COLUMNS = ("key", "value")


@dataclass(frozen=True)
class _CandidateContext:
    candidate: MinuteCandidate
    rule: SessionRule
    slots: tuple[datetime, ...]


@dataclass(frozen=True)
class _ExecutionOperation:
    product: str
    before: PositionState
    after: PositionState
    role_by_contract: Mapping[str, str]
    fill_by_contract: Mapping[str, VwapFill]
    resolution_by_contract: Mapping[str, MultiplierResolution]
    raw_target_by_contract: Mapping[str, float]
    formal_target_by_contract: Mapping[str, float]
    reason: str
    execution_kind: str
    signal_time: datetime | None

    @property
    def timestamp(self) -> datetime:
        return next(iter(self.fill_by_contract.values())).end


def _active_signals(signals: pd.DataFrame) -> dict[date, dict[str, Any]]:
    result: dict[date, dict[str, Any]] = defaultdict(dict)
    ordered = signals.sort_values(["trade_date", "product"], kind="mergesort")
    for row in ordered.itertuples(index=False):
        if int(row.effective_direction) != 0:
            result[row.trade_date][row.product] = row
    return dict(result)


def _add_candidate_role(
    roles: dict[str, str],
    contract: str | None,
    role: str,
) -> None:
    if contract is None:
        return
    if role not in _CANDIDATE_ROLES:
        raise ValueError(f"unsupported dynamic candidate role: {role}")
    current = roles.get(contract)
    if current is None or _ROLE_PRIORITY[role] < _ROLE_PRIORITY[current]:
        roles[contract] = role


def _candidate_roles_for_date(
    *,
    index: int,
    dates: Sequence[date],
    active: Mapping[date, Mapping[str, Any]],
) -> dict[str, str]:
    roles: dict[str, str] = {}
    previous = active.get(dates[index - 1], {}) if index >= 1 else {}
    prior = active.get(dates[index - 2], {}) if index >= 2 else {}

    products = sorted(set(previous) | set(prior))
    for product in products:
        old = prior.get(product)
        new = previous.get(product)
        old_contract = getattr(old, "main_contract", None)
        new_contract = getattr(new, "main_contract", None)
        if old is not None and new is not None:
            if old_contract != new_contract and int(old.effective_direction) == int(
                new.effective_direction
            ):
                _add_candidate_role(roles, old_contract, "roll_old")
                _add_candidate_role(roles, new_contract, "roll_new")
            elif old_contract == new_contract and int(old.effective_direction) == int(
                new.effective_direction
            ):
                _add_candidate_role(roles, new_contract, "carried")
            else:
                _add_candidate_role(roles, old_contract, "exit")
                _add_candidate_role(roles, new_contract, "signal_main")
        elif new is not None:
            _add_candidate_role(roles, new_contract, "signal_main")
        elif old is not None:
            _add_candidate_role(roles, old_contract, "exit")
        if new_contract is not None:
            _add_candidate_role(roles, new_contract, "close_mark")
    return roles


def _execution_roles(
    before: PositionState,
    after: PositionState,
    contracts: set[str],
    *,
    old_contract_override: str | None = None,
) -> dict[str, str]:
    old_contract = old_contract_override or before.contract
    new_contract = after.contract
    before_direction = before.direction or before.locked_direction
    after_direction = after.direction or after.locked_direction
    roles: dict[str, str] = {}
    reversal = (
        before_direction != 0
        and after_direction != 0
        and before_direction != after_direction
    )
    if reversal:
        if old_contract in contracts:
            roles[old_contract] = "exit"
        if new_contract in contracts:
            roles[new_contract] = "signal_main"
    elif old_contract != new_contract and old_contract and new_contract:
        if old_contract in contracts:
            roles[old_contract] = "roll_old"
        if new_contract in contracts:
            roles[new_contract] = "roll_new"
    elif new_contract is None:
        if old_contract in contracts:
            roles[old_contract] = "exit"
    elif old_contract is None:
        roles[new_contract] = "signal_main"
    elif new_contract in contracts:
        roles[new_contract] = "carried"
    if set(roles) != contracts:
        raise ValueError(
            "execution contracts must be exactly the before/after concrete legs"
        )
    return roles


def _prepare_candidates(
    *,
    dates: Sequence[date],
    research: DailyResearch,
    rules: Sequence[SessionRule],
) -> dict[tuple[date, str], _CandidateContext]:
    active = _active_signals(research.signal_result.signals)
    contexts: dict[tuple[date, str], _CandidateContext] = {}
    dynamic_keys: set[tuple[str, str, date]] = set()
    audit_keys: set[tuple[str, str, date]] = set()

    for rule in rules:
        for trade_date in dates:
            if rule.effective_start <= trade_date and (
                rule.effective_end is None or trade_date <= rule.effective_end
            ):
                audit_keys.add((rule.exchange, rule.product, trade_date))

    for index in range(1, len(dates)):
        trade_date = dates[index]
        previous_trade_date = dates[index - 1]
        for contract, role in sorted(
            _candidate_roles_for_date(index=index, dates=dates, active=active).items()
        ):
            product, minute_symbol, exchange = minute_contract_identity(
                contract,
                trade_date,
            )
            key = (exchange, product, trade_date)
            dynamic_keys.add(key)
            if key not in audit_keys:
                raise SessionClockError(
                    exchange=exchange,
                    product=product,
                    trade_date=trade_date,
                    check="dynamic_audit_coverage",
                    reason="dynamic product-day is absent from audited session evidence",
                )
            rule = resolve_session_rule(rules, exchange, product, trade_date)
            slots = build_trading_slots(trade_date, previous_trade_date, rule)
            candidate = MinuteCandidate(
                trade_date=trade_date,
                product=product,
                daily_contract=contract,
                minute_symbol=minute_symbol,
                exchange=exchange,
                window_start=slots[0],
                window_end=slots[-1] + timedelta(minutes=1),
                candidate_role=role,
                causal_in_pool_date=dates[index - 1],
                selection_source="dynamic_daily_research",
            )
            contexts[(trade_date, contract)] = _CandidateContext(
                candidate=candidate,
                rule=rule,
                slots=slots,
            )
    if not dynamic_keys <= audit_keys:
        raise AssertionError("dynamic session keys must be covered before querying")
    return contexts


def _month_key(value: date) -> tuple[int, int]:
    return value.year, value.month


def _monthly_candidates(
    contexts: Mapping[tuple[date, str], _CandidateContext],
) -> dict[tuple[int, int], tuple[MinuteCandidate, ...]]:
    primary: dict[tuple[int, int], list[MinuteCandidate]] = defaultdict(list)
    for context in contexts.values():
        primary[_month_key(context.candidate.trade_date)].append(context.candidate)
    result: dict[tuple[int, int], tuple[MinuteCandidate, ...]] = {}
    previous_last: tuple[MinuteCandidate, ...] = ()
    for month in sorted(primary):
        ordered = tuple(
            sorted(
                primary[month],
                key=lambda item: (item.trade_date, item.product, item.daily_contract),
            )
        )
        combined = {
            (
                item.trade_date,
                item.daily_contract,
            ): item
            for item in (*previous_last, *ordered)
        }
        result[month] = tuple(combined[key] for key in sorted(combined))
        last_date = max(item.trade_date for item in ordered)
        previous_last = tuple(item for item in ordered if item.trade_date == last_date)
    return result


def _dynamic_missing(candidate: MinuteCandidate, *, role: str) -> MinuteDataError:
    return MinuteDataError(
        trade_date=candidate.trade_date,
        product=candidate.product,
        contract=candidate.daily_contract,
        check="dynamic_execution_leg_missing_minutes",
        reason="actual dynamic candidate has no minute rows",
        context={"candidate_role": role},
    )


def _load_month_rows(
    minute_source: Any,
    candidates: Sequence[MinuteCandidate],
) -> tuple[dict[tuple[date, str], pd.DataFrame], int, datetime, datetime]:
    lower = min(candidate.window_start for candidate in candidates)
    upper = max(candidate.window_end for candidate in candidates)
    chunks = list(minute_source.iter_month(candidates, lower, upper))
    if chunks:
        combined = pd.concat(chunks, ignore_index=True)
    else:
        combined = pd.DataFrame()

    identity = ["trade_date", "daily_contract", "bar_time"]
    if not combined.empty:
        duplicates = combined.duplicated(identity, keep=False)
        if duplicates.any():
            duplicate_rows = combined.loc[duplicates]
            conflicting = any(
                len(group.drop_duplicates()) != 1
                for _, group in duplicate_rows.groupby(identity, sort=True)
            )
            if conflicting:
                first = duplicate_rows.iloc[0]
                raise MinuteDataError(
                    trade_date=first["trade_date"],
                    timestamp=first["bar_time"],
                    contract=first["daily_contract"],
                    check="duplicate_overlap_minute_event",
                    reason="monthly overlap returned conflicting minute rows",
                )
            combined = combined.drop_duplicates(identity, keep="first")
        combined = combined.sort_values(
            ["bar_time", "symbol"], kind="mergesort"
        ).reset_index(drop=True)

    frames: dict[tuple[date, str], pd.DataFrame] = {}
    for candidate in candidates:
        if combined.empty:
            continue
        mask = combined["trade_date"].eq(candidate.trade_date) & combined[
            "daily_contract"
        ].eq(candidate.daily_contract)
        frame = combined.loc[mask].copy().reset_index(drop=True)
        if not frame.empty:
            frames[(candidate.trade_date, candidate.daily_contract)] = frame
    return frames, len(combined), lower, upper


def _slot_frame(
    frame: pd.DataFrame,
    slots: Sequence[datetime],
) -> pd.DataFrame:
    return frame.loc[frame["bar_time"].isin(tuple(slots))].copy().reset_index(drop=True)


def _stable_id(*values: object) -> str:
    payload = "\x1f".join(
        value.isoformat() if isinstance(value, (date, datetime)) else repr(value)
        for value in values
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _actual_reason(reason: str, old_weight: float, new_weight: float) -> str:
    if old_weight == 0.0 and new_weight != 0.0 and not reason.startswith("roll"):
        return "entry"
    if old_weight != 0.0 and new_weight == 0.0 and reason == "rebalance":
        return "exit"
    return reason


_SOURCE_AUDIT_FIELDS = (
    "minute_table_min",
    "minute_table_max",
    "minute_query_months",
    "minute_rows",
    "minute_candidate_contract_days",
)
_PLAN_SUMMARY_FIELDS = (
    "query_kind",
    "lower_bound",
    "upper_bound",
    "candidate_contract_days",
    "referenced_chunks",
    "maximum_plan_rows",
    "node_types",
)
_PLAN_CHUNK_NAME = re.compile(r"_hyper_\d+_\d+_chunk")


def _plan_audit_error(
    field: str,
    reason: str,
    *,
    value: object | None = None,
    context: Mapping[str, object] | None = None,
) -> MinuteDataError:
    details: dict[str, object] = {"field": field}
    if value is not None:
        details.update(value=value, value_type=type(value).__name__)
    if context is not None:
        details.update(context)
    return MinuteDataError(
        check="minute_query_plan",
        reason=reason,
        context=details,
    )


def _plan_audit_snapshot(source: Any) -> tuple[object, ...]:
    try:
        payload = source.plan_audit
    except AttributeError as exc:
        raise _plan_audit_error(
            "plan_audit",
            "minute source must expose an immutable plan audit snapshot",
        ) from exc
    if payload is None or callable(payload) or type(payload) is not tuple:
        raise _plan_audit_error(
            "plan_audit",
            "minute source plan audit must be an immutable tuple snapshot",
            value=payload,
        )
    return payload


def _plan_summary_values(payload: object) -> dict[str, object]:
    if payload is None or callable(payload):
        raise _plan_audit_error(
            "summary",
            "minute query plan summary must be a mapping or field object",
            value=payload,
        )
    values: dict[str, object] = {}
    for field in _PLAN_SUMMARY_FIELDS:
        if isinstance(payload, Mapping):
            if field not in payload:
                raise _plan_audit_error(field, "minute query plan field is missing")
            values[field] = payload[field]
        else:
            try:
                values[field] = getattr(payload, field)
            except AttributeError as exc:
                raise _plan_audit_error(
                    field,
                    "minute query plan field is missing",
                ) from exc
    return values


def _validated_plan_summary(
    payload: object,
    *,
    lower: datetime,
    upper: datetime,
    candidate_contract_days: int,
) -> dict[str, object]:
    values = _plan_summary_values(payload)
    if type(values["query_kind"]) is not str or values["query_kind"] != "iter_month":
        raise _plan_audit_error(
            "query_kind",
            "actual monthly query summary must have query_kind='iter_month'",
            value=values["query_kind"],
        )

    parsed_bounds: dict[str, datetime] = {}
    for field in ("lower_bound", "upper_bound"):
        value = values[field]
        if type(value) is not str:
            raise _plan_audit_error(
                field,
                "minute query plan bound must be an ISO datetime string",
                value=value,
            )
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise _plan_audit_error(
                field,
                "minute query plan bound must be an ISO datetime string",
                value=value,
            ) from exc
        if not _is_timezone_aware(parsed):
            raise _plan_audit_error(
                field,
                "minute query plan bound must include a timezone offset",
                value=value,
            )
        parsed_bounds[field] = parsed
    try:
        invalid_order = parsed_bounds["lower_bound"] >= parsed_bounds["upper_bound"]
    except (TypeError, ValueError, OverflowError) as exc:
        raise _plan_audit_error(
            "lower_bound",
            "minute query plan bounds must be mutually comparable",
        ) from exc
    if invalid_order:
        raise _plan_audit_error(
            "lower_bound",
            "minute query plan lower bound must precede its upper bound",
        )
    for field, expected in (("lower_bound", lower), ("upper_bound", upper)):
        if values[field] != expected.isoformat():
            raise _plan_audit_error(
                field,
                "minute query plan bound does not match the actual monthly query",
                value=values[field],
                context={"expected": expected.isoformat()},
            )

    count = values["candidate_contract_days"]
    if type(count) is not int or count <= 0:
        raise _plan_audit_error(
            "candidate_contract_days",
            "minute query plan candidate count must be a positive integer",
            value=count,
        )
    if count != candidate_contract_days:
        raise _plan_audit_error(
            "candidate_contract_days",
            "minute query plan candidate count does not match the actual query",
            value=count,
            context={"expected": candidate_contract_days},
        )

    chunks = values["referenced_chunks"]
    if type(chunks) is not tuple:
        raise _plan_audit_error(
            "referenced_chunks",
            "minute query plan chunk names must be an immutable tuple",
            value=chunks,
        )
    if len(chunks) > 3:
        raise _plan_audit_error(
            "referenced_chunks",
            "minute query plan must not reference more than three Timescale chunks",
            value=chunks,
            context={"chunk_count": len(chunks)},
        )
    for chunk in chunks:
        if type(chunk) is not str or _PLAN_CHUNK_NAME.fullmatch(chunk) is None:
            raise _plan_audit_error(
                "referenced_chunks",
                "minute query plan contains an invalid Timescale chunk name",
                value=chunk,
            )
    if chunks != tuple(sorted(set(chunks))):
        raise _plan_audit_error(
            "referenced_chunks",
            "minute query plan chunk names must be unique and sorted",
            value=chunks,
        )

    maximum_rows = values["maximum_plan_rows"]
    if (
        type(maximum_rows) not in (int, float)
        or (type(maximum_rows) is float and not math.isfinite(maximum_rows))
        or maximum_rows < 0
        or maximum_rows >= 10_000_000
    ):
        raise _plan_audit_error(
            "maximum_plan_rows",
            "minute query plan maximum row estimate must be finite and safely bounded",
            value=maximum_rows,
        )

    node_types = values["node_types"]
    if type(node_types) is not tuple or not node_types:
        raise _plan_audit_error(
            "node_types",
            "minute query plan node types must be a nonempty immutable tuple",
            value=node_types,
        )
    for node_type in node_types:
        if (
            type(node_type) is not str
            or not node_type.strip()
            or node_type.strip() != node_type
        ):
            raise _plan_audit_error(
                "node_types",
                "minute query plan contains an invalid node type",
                value=node_type,
            )
    if node_types != tuple(sorted(set(node_types))):
        raise _plan_audit_error(
            "node_types",
            "minute query plan node types must be unique and sorted",
            value=node_types,
        )
    return values


def _query_plan_quality_record(
    values: Mapping[str, object],
    *,
    query_month: str,
) -> dict[str, object]:
    return {
        "check": "minute_query_plan",
        "trade_date": None,
        "product": None,
        "contract": None,
        "candidate_role": None,
        "observed_rows": values["candidate_contract_days"],
        "missing_slots": None,
        "query_month": query_month,
        "detail": json.dumps(
            dict(values),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ),
    }


def _plan_summary_for_query(
    source: Any,
    *,
    initial_count: int,
    actual_monthly_queries: int,
    lower: datetime,
    upper: datetime,
    candidate_contract_days: int,
) -> dict[str, object]:
    snapshot = _plan_audit_snapshot(source)
    plan_summary_count = len(snapshot) - initial_count
    if plan_summary_count != actual_monthly_queries:
        raise MinuteDataError(
            check="minute_query_plan",
            reason="source plan audit count does not match actual monthly queries",
            context={
                "actual_monthly_queries": actual_monthly_queries,
                "plan_summary_count": plan_summary_count,
            },
        )
    return _validated_plan_summary(
        snapshot[-1],
        lower=lower,
        upper=upper,
        candidate_contract_days=candidate_contract_days,
    )


def _source_audit_error(
    field: str,
    reason: str,
    *,
    value: object | None = None,
) -> MinuteDataError:
    context: dict[str, object] = {"field": field}
    if value is not None:
        context.update(value=value, value_type=type(value).__name__)
    return MinuteDataError(
        check="minute_source_audit",
        reason=reason,
        context=context,
    )


def _validated_source_audit(source: Any) -> dict[str, object]:
    try:
        payload = source.audit
    except AttributeError as exc:
        raise _source_audit_error(
            "audit",
            "minute source must expose the Task 10 audit contract",
        ) from exc
    if payload is None or callable(payload):
        raise _source_audit_error(
            "audit",
            "minute source audit must be a mapping or field object",
            value=payload,
        )

    values: dict[str, object] = {}
    for field in _SOURCE_AUDIT_FIELDS:
        if isinstance(payload, Mapping):
            if field not in payload:
                raise _source_audit_error(field, "minute source audit field is missing")
            values[field] = payload[field]
        else:
            try:
                values[field] = getattr(payload, field)
            except AttributeError as exc:
                raise _source_audit_error(
                    field,
                    "minute source audit field is missing",
                ) from exc

    for field in ("minute_table_min", "minute_table_max"):
        value = values[field]
        if not _is_timezone_aware(value):
            raise _source_audit_error(
                field,
                "minute table bound must be a timezone-aware datetime",
                value=value,
            )
    if values["minute_table_min"] > values["minute_table_max"]:
        raise _source_audit_error(
            "minute_table_min",
            "minute table minimum must not follow its maximum",
        )
    for field in _SOURCE_AUDIT_FIELDS[2:]:
        value = values[field]
        if type(value) is not int or value < 0:
            raise _source_audit_error(
                field,
                "minute source audit counter must be a nonnegative integer",
                value=value,
            )
    return values


class CarryMinuteBacktester:
    """Run Carry through authoritative minute clocks and piecewise accounts."""

    def __init__(
        self,
        *,
        data: CarryDataSet,
        minute_source: Any,
        session_rules: Sequence[SessionRule],
        config: CarryConfig,
        start: date,
        end: date,
    ) -> None:
        if start > end:
            raise ValueError("start must be on or before end")
        self.data = data
        self.minute_source = minute_source
        self.session_rules = tuple(session_rules)
        self.config = config
        self.start = start
        self.end = end

    def run(self) -> CarryBacktestResult:
        validate_capture_coverage(
            capture_start=SESSION_RULES_CAPTURE_START,
            backtest_start=self.start,
            prewarm_calendar_days=self.config.prewarm_calendar_days,
        )
        initial_plan_summary_count = len(_plan_audit_snapshot(self.minute_source))
        prices = self.data.prices.loc[self.data.prices["trade_date"] <= self.end].copy()
        dates = sorted(prices["trade_date"].dropna().unique().tolist())
        if not dates:
            raise ValueError("no Carry prices on or before end")
        report_dates = [trade_date for trade_date in dates if trade_date >= self.start]
        if not report_dates:
            raise ValueError("no strategy trading day on or after start")
        query_start = dates[0]
        report_start_date = report_dates[0]

        research = build_daily_research(prices, self.config)
        contexts = _prepare_candidates(
            dates=dates,
            research=research,
            rules=self.session_rules,
        )
        month_candidates = _monthly_candidates(contexts)
        signals_by_date = {
            trade_date: frame.reset_index(drop=True)
            for trade_date, frame in research.signal_result.signals.groupby(
                "trade_date", sort=True
            )
        }
        empty_signals = pd.DataFrame(columns=research.signal_result.signals.columns)
        atr_lookup = {
            (row.trade_date, row.contract): row.atr
            for row in research.contract_atr.itertuples(index=False)
        }
        daily_by_date = {
            trade_date: frame.reset_index(drop=True)
            for trade_date, frame in prices.groupby("trade_date", sort=True)
        }
        contract_products = _contract_products(prices)

        formal = EventAccount(cost_bps=self.config.cost_bps)
        shadow_account = EventAccount(cost_bps=self.config.cost_bps)
        stop_machine = IntradayStopMachine(self.config)
        shadow_window = ShadowVolWindow(self.config)
        shadow_interval_enabled = False
        states: dict[str, PositionState] = {}
        pending_plan: TargetPlan | None = None
        pending_formal: dict[str, float] = {}
        pending_scale_ready = False
        pending_signal_time: datetime | None = None
        vol_ready_date: date | None = None
        mark_prices: dict[str, float] = {}
        resolution_cache: dict[tuple[date, str], MultiplierResolution] = {}
        execution_ids: set[str] = set()
        audited_fill_windows: set[tuple[date, str, datetime, datetime]] = set()
        report_equity = 1.0

        daily_records: list[dict[str, object]] = []
        position_records: list[dict[str, object]] = []
        execution_records: list[dict[str, object]] = []
        stop_records: list[dict[str, object]] = []
        quality_records: list[dict[str, object]] = []
        pending_final_stop_records: dict[str, dict[str, object]] = {}
        query_months: list[str] = []
        current_month: tuple[int, int] | None = None
        current_frames: dict[tuple[date, str], pd.DataFrame] = {}
        current_daily_opens: dict[str, float] = {}

        def require_context(
            trade_date: date,
            contract: str,
            role: str,
        ) -> _CandidateContext:
            context = contexts.get((trade_date, contract))
            if context is None:
                product = contract_products.get(contract)
                raise MinuteDataError(
                    trade_date=trade_date,
                    product=product,
                    contract=contract,
                    check="dynamic_execution_leg_missing_minutes",
                    reason="actual dynamic candidate was absent before monthly query",
                    context={"candidate_role": role},
                )
            return context

        def require_frame(
            trade_date: date,
            contract: str,
            role: str,
        ) -> tuple[_CandidateContext, pd.DataFrame]:
            context = require_context(trade_date, contract, role)
            frame = current_frames.get((trade_date, contract))
            if frame is None or frame.empty:
                raise _dynamic_missing(context.candidate, role=role)
            return context, frame

        def resolution(
            trade_date: date,
            contract: str,
            frame: pd.DataFrame,
        ) -> MultiplierResolution:
            key = (trade_date, contract)
            cached = resolution_cache.get(key)
            if cached is None:
                cached = self.minute_source.resolve_metadata_multiplier(
                    daily_contract=contract,
                    trade_date=trade_date,
                    frame=frame,
                )
                resolution_cache[key] = cached
            return cached

        def audit_fill(
            fill: VwapFill,
            *,
            trade_date: date,
            product: str,
            role: str,
        ) -> None:
            key = (trade_date, fill.contract, fill.start, fill.end)
            if fill.missing_slots <= 0 or key in audited_fill_windows:
                return
            audited_fill_windows.add(key)
            quality_records.append(
                {
                    "check": "five_minute_fill_missing_slots",
                    "trade_date": trade_date,
                    "product": product,
                    "contract": fill.contract,
                    "candidate_role": role,
                    "observed_rows": fill.traded_rows,
                    "missing_slots": fill.missing_slots,
                    "query_month": f"{trade_date:%Y-%m}",
                    "detail": fill.start.isoformat(),
                }
            )

        def fill_for(
            trade_date: date,
            contract: str,
            role: str,
            slots: Sequence[datetime],
        ) -> tuple[VwapFill, MultiplierResolution]:
            context, frame = require_frame(trade_date, contract, role)
            resolved = resolution(trade_date, contract, frame)
            minute_symbol = minute_contract_identity(contract, trade_date)[1]
            fill = five_minute_vwap(
                _slot_frame(frame, slots),
                slots=slots,
                contract=minute_symbol,
                multiplier=resolved.multiplier,
            )
            fill = replace(fill, contract=contract)
            audit_fill(
                fill,
                trade_date=trade_date,
                product=context.candidate.product,
                role=role,
            )
            return fill, resolved

        def event_prices(
            account: EventAccount,
            trade_date: date,
            timestamp: datetime,
            overrides: Mapping[str, float],
            target: Mapping[str, float],
        ) -> dict[str, float]:
            required = set(account.weights) | set(target)
            result: dict[str, float] = {}
            for contract in sorted(required):
                if contract in overrides:
                    result[contract] = float(overrides[contract])
                    continue
                _, frame = require_frame(trade_date, contract, "carried")
                eligible = frame.loc[
                    frame["bar_time"].lt(timestamp) & frame["volume"].gt(0)
                ]
                if not eligible.empty:
                    result[contract] = float(eligible.iloc[-1]["close"])
                elif contract in mark_prices:
                    result[contract] = mark_prices[contract]
                else:
                    raise MinuteDataError(
                        trade_date=trade_date,
                        timestamp=timestamp,
                        product=contract_products.get(contract),
                        contract=contract,
                        check="dynamic_execution_leg_missing_minutes",
                        reason="held leg has no price at execution event",
                        context={"candidate_role": "carried"},
                    )
            return result

        def append_executions(
            *,
            trade_date: date,
            account_event: AccountEvent,
            operations: Sequence[_ExecutionOperation],
        ) -> dict[tuple[str, str], str]:
            operation_by_contract = {
                contract: operation
                for operation in operations
                for contract in operation.fill_by_contract
            }
            linked: dict[tuple[str, str], str] = {}
            for record in account_event.executions:
                operation = operation_by_contract[record.contract]
                fill = operation.fill_by_contract[record.contract]
                resolved = operation.resolution_by_contract[record.contract]
                role = operation.role_by_contract[record.contract]
                reason = _actual_reason(
                    record.reason,
                    record.old_weight,
                    record.new_weight,
                )
                execution_id = _stable_id(
                    trade_date,
                    operation.product,
                    record.contract,
                    fill.start,
                    fill.end,
                    reason,
                    record.old_weight,
                    record.new_weight,
                )
                if execution_id in execution_ids:
                    raise MinuteDataError(
                        trade_date=trade_date,
                        timestamp=fill.end,
                        product=operation.product,
                        contract=record.contract,
                        check="duplicate_execution_id",
                        reason="stable execution ID was emitted more than once",
                        context={"execution_id": execution_id},
                    )
                execution_ids.add(execution_id)
                linked[(operation.product, record.contract)] = execution_id
                execution_records.append(
                    {
                        "execution_id": execution_id,
                        "trade_date": trade_date,
                        "product": operation.product,
                        "contract": record.contract,
                        "candidate_role": role,
                        "reason": reason,
                        "execution_kind": operation.execution_kind,
                        "signal_time": operation.signal_time,
                        "window_start": fill.start,
                        "window_end": fill.end,
                        "vwap": fill.price,
                        "daily_open": current_daily_opens[record.contract],
                        "old_weight": record.old_weight,
                        "new_weight": record.new_weight,
                        "weight_change": record.weight_change,
                        "turnover": record.turnover,
                        "cost": record.cost,
                        "volume": fill.volume,
                        "amount": fill.amount,
                        "missing_slots": fill.missing_slots,
                        "multiplier": fill.multiplier,
                        "multiplier_source": resolved.source,
                        "multiplier_pass_rate": resolved.pass_rate,
                        "multiplier_sample_rows": resolved.sample_rows,
                    }
                )
            return linked

        def apply_operations(
            trade_date: date,
            operations: Sequence[_ExecutionOperation],
        ) -> dict[tuple[str, str], str]:
            raw_target = dict(shadow_account.weights)
            formal_target = dict(formal.weights)
            fills: dict[str, VwapFill] = {}
            reasons: dict[str, str] = {}
            for operation in operations:
                for contract, value in operation.raw_target_by_contract.items():
                    if value == 0.0:
                        raw_target.pop(contract, None)
                    else:
                        raw_target[contract] = value
                for contract, value in operation.formal_target_by_contract.items():
                    if value == 0.0:
                        formal_target.pop(contract, None)
                    else:
                        formal_target[contract] = value
                fills.update(operation.fill_by_contract)
                for contract in operation.fill_by_contract:
                    reasons[contract] = operation.reason
            timestamp = operations[0].timestamp
            overrides = {contract: fill.price for contract, fill in fills.items()}
            shadow_event = shadow_account.rebalance(
                timestamp,
                event_prices(
                    shadow_account,
                    trade_date,
                    timestamp,
                    overrides,
                    raw_target,
                ),
                raw_target,
                {
                    contract: _actual_reason(
                        reasons[contract],
                        float(shadow_account.weights.get(contract, 0.0)),
                        float(raw_target.get(contract, 0.0)),
                    )
                    for contract in set(shadow_account.weights) | set(raw_target)
                    if shadow_account.weights.get(contract, 0.0)
                    != raw_target.get(contract, 0.0)
                },
            )
            del shadow_event
            formal_event = formal.rebalance(
                timestamp,
                event_prices(
                    formal,
                    trade_date,
                    timestamp,
                    overrides,
                    formal_target,
                ),
                formal_target,
                {
                    contract: _actual_reason(
                        reasons[contract],
                        float(formal.weights.get(contract, 0.0)),
                        float(formal_target.get(contract, 0.0)),
                    )
                    for contract in set(formal.weights) | set(formal_target)
                    if formal.weights.get(contract, 0.0)
                    != formal_target.get(contract, 0.0)
                },
            )
            mark_prices.update(overrides)
            return append_executions(
                trade_date=trade_date,
                account_event=formal_event,
                operations=operations,
            )

        for index, trade_date in enumerate(dates):
            month = _month_key(trade_date)
            if month != current_month:
                current_month = month
                current_frames = {}
                candidates = month_candidates.get(month, ())
                if candidates:
                    current_frames, _, lower, upper = _load_month_rows(
                        self.minute_source,
                        candidates,
                    )
                    query_months.append(f"{month[0]:04d}-{month[1]:02d}")
                    plan_values = _plan_summary_for_query(
                        self.minute_source,
                        initial_count=initial_plan_summary_count,
                        actual_monthly_queries=len(query_months),
                        lower=lower,
                        upper=upper,
                        candidate_contract_days=len(candidates),
                    )
                    quality_records.append(
                        _query_plan_quality_record(
                            plan_values,
                            query_month=query_months[-1],
                        )
                    )
                    for candidate in candidates:
                        context = contexts[
                            (candidate.trade_date, candidate.daily_contract)
                        ]
                        frame = current_frames.get(
                            (candidate.trade_date, candidate.daily_contract)
                        )
                        if frame is None or frame.empty:
                            quality_records.append(
                                {
                                    "check": "candidate_envelope_no_rows",
                                    "trade_date": candidate.trade_date,
                                    "product": candidate.product,
                                    "contract": candidate.daily_contract,
                                    "candidate_role": candidate.candidate_role,
                                    "observed_rows": 0,
                                    "missing_slots": len(context.slots),
                                    "query_month": query_months[-1],
                                    "detail": "static candidate removed unless runtime state accesses it",
                                }
                            )
                            continue
                        observed_slots = set(frame["bar_time"])
                        missing_slots = len(set(context.slots) - observed_slots)
                        zero_volume_rows = frame.loc[
                            frame["bar_time"].isin(context.slots)
                            & frame["volume"].eq(0.0)
                        ]
                        quality_records.append(
                            {
                                "check": "candidate_coverage",
                                "trade_date": candidate.trade_date,
                                "product": candidate.product,
                                "contract": candidate.daily_contract,
                                "candidate_role": candidate.candidate_role,
                                "observed_rows": len(frame),
                                "missing_slots": missing_slots,
                                "query_month": query_months[-1],
                                "detail": "covered",
                            }
                        )
                        if not zero_volume_rows.empty:
                            quality_records.append(
                                {
                                    "check": "zero_volume_minute_slots",
                                    "trade_date": candidate.trade_date,
                                    "product": candidate.product,
                                    "contract": candidate.daily_contract,
                                    "candidate_role": candidate.candidate_role,
                                    "observed_rows": len(zero_volume_rows),
                                    "missing_slots": 0,
                                    "query_month": query_months[-1],
                                    "detail": "authoritative slots with zero volume",
                                }
                            )

            day_prices = daily_by_date[trade_date]
            current_daily_opens = {
                row.contract: float(row.open)
                for row in day_prices.itertuples(index=False)
            }
            closes = {
                row.contract: float(row.close)
                for row in day_prices.itertuples(index=False)
            }
            if index == 0:
                formal.initialize(closes)
                shadow_account.initialize(closes)
                mark_prices.update(closes)
            carried_formal = dict(formal.weights)
            scale_ready_for_day = pending_scale_ready
            estimate_before_day = shadow_window.estimate()

            scheduled: dict[datetime, list[_ExecutionOperation]] = defaultdict(list)
            prospective_states = dict(states)
            if pending_plan is not None:
                products = sorted(set(states) | set(pending_plan.states))
                for product in products:
                    before = states.get(product, PositionState())
                    after = pending_plan.states.get(product, PositionState())
                    prospective_states[product] = after
                    pending_stop = pending_final_stop_records.get(product)
                    old_contract = before.contract
                    if old_contract is None and pending_stop is not None:
                        old_contract = str(pending_stop["contract"])
                    new_contract = after.contract
                    involved = {
                        contract
                        for contract in (old_contract, new_contract)
                        if contract is not None
                    }
                    changed = {
                        contract
                        for contract in involved
                        if (
                            shadow_account.weights.get(contract, 0.0)
                            != pending_plan.raw_weights.get(contract, 0.0)
                            or formal.weights.get(contract, 0.0)
                            != pending_formal.get(contract, 0.0)
                        )
                    }
                    if not changed:
                        stop_machine.reset_for_transition(
                            product,
                            before,
                            after,
                            fill_end=None,
                        )
                        states[product] = after
                        continue
                    anchor = new_contract or old_contract
                    assert anchor is not None
                    context = require_context(trade_date, anchor, "signal_main")
                    execution_slots = context.slots[:5]
                    fill_by_contract: dict[str, VwapFill] = {}
                    resolved_by_contract: dict[str, MultiplierResolution] = {}
                    role_by_contract = _execution_roles(
                        before,
                        after,
                        changed,
                        old_contract_override=old_contract,
                    )
                    for contract in sorted(changed):
                        role = role_by_contract[contract]
                        fill, resolved = fill_for(
                            trade_date,
                            contract,
                            role,
                            execution_slots,
                        )
                        fill_by_contract[contract] = fill
                        resolved_by_contract[contract] = resolved
                    operation = _ExecutionOperation(
                        product=product,
                        before=before,
                        after=after,
                        role_by_contract=role_by_contract,
                        fill_by_contract=fill_by_contract,
                        resolution_by_contract=resolved_by_contract,
                        raw_target_by_contract={
                            contract: pending_plan.raw_weights.get(contract, 0.0)
                            for contract in changed
                        },
                        formal_target_by_contract={
                            contract: pending_formal.get(contract, 0.0)
                            for contract in changed
                        },
                        reason=pending_plan.reasons.get(product, "rebalance"),
                        signal_time=pending_signal_time,
                        execution_kind="daily_target",
                    )
                    scheduled[operation.timestamp].append(operation)

            bar_events: dict[
                datetime,
                list[tuple[str, FifteenMinuteBar, tuple[datetime, ...] | None]],
            ] = defaultdict(list)
            if index > 0:
                for product, state in sorted(prospective_states.items()):
                    if state.direction == 0 or state.contract is None:
                        continue
                    context, frame = require_frame(
                        trade_date,
                        state.contract,
                        "carried",
                    )
                    minute_symbol = context.candidate.minute_symbol
                    for bucket in fifteen_minute_buckets(context.slots, context.rule):
                        bar = aggregate_fifteen_minute_bar(
                            _slot_frame(frame, bucket),
                            slots=bucket,
                            contract=minute_symbol,
                        )
                        bar = replace(bar, contract=state.contract)
                        if bar.missing_slots > 0:
                            quality_records.append(
                                {
                                    "check": "partial_fifteen_minute_bar",
                                    "trade_date": trade_date,
                                    "product": product,
                                    "contract": state.contract,
                                    "candidate_role": "carried",
                                    "observed_rows": bar.traded_rows,
                                    "missing_slots": bar.missing_slots,
                                    "query_month": f"{trade_date:%Y-%m}",
                                    "detail": bar.start.isoformat(),
                                }
                            )
                        try:
                            fill_slots = next_slots(context.slots, bar.end, 5)
                        except SessionClockError as exc:
                            if exc.check == "next_slots_count":
                                fill_slots = None
                            else:
                                raise
                        bar_events[bar.end].append((product, bar, fill_slots))

            final_decisions: dict[str, StopDecision] = {}
            stop_metadata: dict[tuple[datetime, str], StopDecision] = {}
            event_times = set(scheduled) | set(bar_events)
            while event_times:
                event_time = min(event_times)
                event_times.remove(event_time)
                operations = tuple(scheduled.pop(event_time, ()))
                if operations:
                    linked = apply_operations(trade_date, operations)
                    for operation in operations:
                        stop_machine.reset_for_transition(
                            operation.product,
                            operation.before,
                            operation.after,
                            fill_end=event_time,
                        )
                        states[operation.product] = operation.after
                        pending_stop = pending_final_stop_records.pop(
                            operation.product,
                            None,
                        )
                        if pending_stop is not None:
                            stopped_contract = str(pending_stop["contract"])
                            pending_stop["execution_id"] = linked.get(
                                (operation.product, stopped_contract)
                            )
                        decision = stop_metadata.pop(
                            (event_time, operation.product),
                            None,
                        )
                        if decision is None:
                            continue
                        contract = operation.before.contract
                        assert contract is not None
                        stop_records.append(
                            {
                                "trade_date": trade_date,
                                "product": operation.product,
                                "contract": contract,
                                "bar_start": decision.bar.start,
                                "bar_end": decision.bar.end,
                                "open": decision.bar.open,
                                "high": decision.bar.high,
                                "low": decision.bar.low,
                                "close": decision.bar.close,
                                "atr": atr_lookup[(dates[index - 1], contract)],
                                "threshold": decision.threshold,
                                "tranches_before": operation.before.tranches_remaining,
                                "tranches_after": operation.after.tranches_remaining,
                                "locked_direction": operation.after.locked_direction,
                                "triggered": True,
                                "execution_id": linked.get(
                                    (operation.product, contract)
                                ),
                            }
                        )

                for product, bar, fill_slots in bar_events.pop(event_time, ()):
                    before = states.get(product, PositionState())
                    if before.direction == 0 or before.contract != bar.contract:
                        continue
                    previous_trade_date = dates[index - 1]
                    atr = atr_lookup.get((previous_trade_date, before.contract))
                    try:
                        valid_atr = math.isfinite(float(atr)) and float(atr) > 0.0
                    except (TypeError, ValueError, OverflowError):
                        valid_atr = False
                    if not valid_atr:
                        raise MinuteDataError(
                            trade_date=trade_date,
                            product=product,
                            contract=before.contract,
                            check="prior_day_atr",
                            reason="intraday stop requires finite positive ATR labeled T-1",
                            context={"atr_date": previous_trade_date, "atr": atr},
                        )
                    is_final_bar = fill_slots is None
                    fill_end = (
                        bar.end + timedelta(microseconds=1)
                        if is_final_bar
                        else fill_slots[-1] + timedelta(minutes=1)
                    )
                    decision = stop_machine.on_bar(
                        trade_date,
                        product,
                        before,
                        bar,
                        float(atr),
                        fill_end,
                    )
                    if not decision.triggered:
                        states[product] = decision.state
                        if bar.no_trade:
                            quality_records.append(
                                {
                                    "check": "no_trade_fifteen_minute_bar",
                                    "trade_date": trade_date,
                                    "product": product,
                                    "contract": before.contract,
                                    "candidate_role": "carried",
                                    "observed_rows": bar.traded_rows,
                                    "missing_slots": bar.missing_slots,
                                    "query_month": f"{trade_date:%Y-%m}",
                                    "detail": bar.start.isoformat(),
                                }
                            )
                        continue
                    if is_final_bar:
                        states[product] = decision.state
                        final_decisions[product] = decision
                        record = {
                            "trade_date": trade_date,
                            "product": product,
                            "contract": before.contract,
                            "bar_start": decision.bar.start,
                            "bar_end": decision.bar.end,
                            "open": decision.bar.open,
                            "high": decision.bar.high,
                            "low": decision.bar.low,
                            "close": decision.bar.close,
                            "atr": float(atr),
                            "threshold": decision.threshold,
                            "tranches_before": before.tranches_remaining,
                            "tranches_after": decision.state.tranches_remaining,
                            "locked_direction": decision.state.locked_direction,
                            "triggered": True,
                            "execution_id": None,
                        }
                        stop_records.append(record)
                        pending_final_stop_records[product] = record
                        continue
                    assert fill_slots is not None
                    _, frame = require_frame(
                        trade_date,
                        before.contract,
                        "carried",
                    )
                    resolved = resolution(trade_date, before.contract, frame)
                    minute_symbol = minute_contract_identity(
                        before.contract, trade_date
                    )[1]
                    fill = five_minute_vwap(
                        _slot_frame(frame, fill_slots),
                        slots=fill_slots,
                        contract=minute_symbol,
                        multiplier=resolved.multiplier,
                    )
                    fill = replace(fill, contract=before.contract)
                    audit_fill(
                        fill,
                        trade_date=trade_date,
                        product=product,
                        role="carried",
                    )
                    after = decision.state
                    ratio = (
                        after.tranches_remaining / before.tranches_remaining
                        if before.tranches_remaining
                        else 0.0
                    )
                    operation = _ExecutionOperation(
                        product=product,
                        before=before,
                        after=after,
                        role_by_contract={before.contract: "carried"},
                        fill_by_contract={before.contract: fill},
                        resolution_by_contract={before.contract: resolved},
                        raw_target_by_contract={
                            before.contract: shadow_account.weights.get(
                                before.contract, 0.0
                            )
                            * ratio
                        },
                        formal_target_by_contract={
                            before.contract: formal.weights.get(before.contract, 0.0)
                            * ratio
                        },
                        reason=f"stop_{decision.stage}",
                        signal_time=event_time,
                        execution_kind="intraday_stop",
                    )
                    scheduled[fill.end].append(operation)
                    stop_metadata[(fill.end, product)] = decision
                    event_times.add(fill.end)

            held_contracts = set(formal.weights) | set(shadow_account.weights)
            missing_closes = sorted(held_contracts - set(closes))
            if missing_closes:
                contract = missing_closes[0]
                raise MinuteDataError(
                    trade_date=trade_date,
                    product=contract_products.get(contract),
                    contract=contract,
                    check="daily_close_mark",
                    reason="active execution leg has no futures_daily.close",
                    context={"candidate_role": "close_mark"},
                )
            close_timestamp = datetime.combine(
                trade_date,
                time(15, 0),
                tzinfo=SHANGHAI,
            )
            shadow_account.mark_close(
                trade_date,
                close_timestamp,
                {contract: closes[contract] for contract in held_contracts},
            )
            formal.mark_close(
                trade_date,
                close_timestamp,
                {contract: closes[contract] for contract in held_contracts},
            )
            shadow_daily = shadow_account.drain_daily_row(trade_date, "ordinary")
            formal_daily = formal.drain_daily_row(trade_date, "ordinary")
            mark_prices.update(closes)
            if shadow_interval_enabled:
                shadow_window.append(
                    shadow_daily.net_return,
                    active=shadow_daily.gross_leverage > 0.0,
                )
            estimate = shadow_window.estimate()

            if trade_date == report_start_date and (
                not scale_ready_for_day or not estimate_before_day.ready
            ):
                raise WarmupInsufficientError(
                    query_start=query_start,
                    report_start_date=report_start_date,
                    signal_ready_date=research.signal_result.signal_ready_date,
                    shadow_observations=estimate_before_day.observations,
                    active_days=estimate_before_day.active_days,
                    required_observations=self.config.vol_window,
                    required_active_days=self.config.min_shadow_active_days,
                )

            if trade_date >= report_start_date:
                report_equity *= 1.0 + formal_daily.net_return
                daily_records.append(
                    {
                        "trade_date": trade_date,
                        "gross_return": formal_daily.gross_return,
                        "turnover": formal_daily.turnover,
                        "cost": formal_daily.cost,
                        "net_return": formal_daily.net_return,
                        "equity": report_equity,
                        "gross_leverage": formal_daily.gross_leverage,
                        "boundary_type": (
                            "report_start_initialization"
                            if trade_date == report_start_date
                            else "ordinary"
                        ),
                    }
                )
                position_records.extend(
                    _position_rows(
                        trade_date=trade_date,
                        states=states,
                        raw_weights=dict(shadow_account.weights),
                        formal_weights=dict(formal.weights),
                        carried_weights=carried_formal,
                        report_start_date=report_start_date,
                    )
                )

            day_signals = signals_by_date.get(trade_date, empty_signals)
            pending_plan = merge_close_plan(
                states,
                day_signals,
                self.config,
                final_stop_decisions=final_decisions,
            )
            shadow_interval_enabled = (
                pending_signal_time is not None
                and research.signal_result.signal_ready_date is not None
                and pending_signal_time.date()
                >= research.signal_result.signal_ready_date
            )
            pending_signal_time = close_timestamp
            pending_scale_ready = estimate.ready
            if estimate.ready:
                pending_formal = scale_weights(
                    pending_plan.raw_weights,
                    estimate.vol_scale,
                    self.config,
                )
                if vol_ready_date is None and index + 1 < len(dates):
                    vol_ready_date = dates[index + 1]
            else:
                pending_formal = {}

        daily = _records_frame(daily_records, _DAILY_COLUMNS)
        positions = _records_frame(position_records, _POSITION_COLUMNS)
        executions = _records_frame(execution_records, _EXECUTION_COLUMNS)
        stops = _records_frame(stop_records, _STOP_COLUMNS)
        minute_quality = _records_frame(quality_records, _QUALITY_COLUMNS)
        reported_executions = executions.loc[
            executions["trade_date"] >= report_start_date
        ].reset_index(drop=True)
        trade_records = [
            {
                "trade_date": row.trade_date,
                "product": row.product,
                "contract": row.contract,
                "old_weight": row.old_weight,
                "new_weight": row.new_weight,
                "weight_change": row.weight_change,
                "reason": row.reason,
            }
            for row in reported_executions.itertuples(index=False)
        ]
        trades = _records_frame(trade_records, _TRADE_COLUMNS)
        session_versions = sorted({rule.version for rule in self.session_rules})
        if session_versions != [SESSION_RULES_VERSION]:
            raise ValueError("session rules must use the repository rules version")
        source_audit = _validated_source_audit(self.minute_source)
        final_plan_summary_count = (
            len(_plan_audit_snapshot(self.minute_source))
            - initial_plan_summary_count
        )
        actual_monthly_queries = len(query_months)
        if not (
            final_plan_summary_count
            == actual_monthly_queries
            == source_audit["minute_query_months"]
        ):
            raise MinuteDataError(
                check="minute_query_plan",
                reason=(
                    "actual monthly queries, plan summaries and source audit count "
                    "must match"
                ),
                context={
                    "actual_monthly_queries": actual_monthly_queries,
                    "plan_summary_count": final_plan_summary_count,
                    "source_audit_minute_query_months": source_audit[
                        "minute_query_months"
                    ],
                },
            )
        config_rows: list[dict[str, object]] = [
            {"key": "requested_start", "value": self.start},
            {"key": "requested_end", "value": self.end},
            {"key": "query_start", "value": query_start},
            {"key": "report_start_date", "value": report_start_date},
            {
                "key": "signal_ready_date",
                "value": research.signal_result.signal_ready_date,
            },
            {"key": "vol_ready_date", "value": vol_ready_date},
            {"key": "execution_mode", "value": "minute"},
            {"key": "accounting_clock", "value": "piecewise_close_marked"},
            {
                "key": "minute_query_rules_version",
                "value": MINUTE_QUERY_RULES_VERSION,
            },
            {"key": "session_rules_version", "value": SESSION_RULES_VERSION},
            {
                "key": "multiplier_resolution_version",
                "value": MULTIPLIER_RESOLUTION_VERSION,
            },
            {
                "key": "minute_table_min",
                "value": source_audit["minute_table_min"].isoformat(),
            },
            {
                "key": "minute_table_max",
                "value": source_audit["minute_table_max"].isoformat(),
            },
            {
                "key": "minute_query_months",
                "value": source_audit["minute_query_months"],
            },
            {
                "key": "minute_rows",
                "value": source_audit["minute_rows"],
            },
            {
                "key": "minute_candidate_contract_days",
                "value": source_audit["minute_candidate_contract_days"],
            },
        ]
        config_rows.extend(
            {"key": key, "value": value} for key, value in asdict(self.config).items()
        )
        return CarryBacktestResult(
            daily_returns=daily,
            positions=positions,
            trades=trades,
            signals=research.signal_result.signals.reset_index(drop=True),
            curve_selection=research.curve_result.audit.reset_index(drop=True),
            data_quality=self.data.data_quality.reset_index(drop=True),
            run_config=_records_frame(config_rows, _RUN_CONFIG_COLUMNS),
            metrics=_summary_metrics(daily),
            executions=reported_executions,
            intraday_stops=stops.loc[
                stops["trade_date"] >= report_start_date
            ].reset_index(drop=True),
            minute_data_quality=minute_quality,
            execution_mode="minute",
        )
