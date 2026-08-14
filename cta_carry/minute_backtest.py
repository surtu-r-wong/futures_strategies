"""Intraday stop decisions and close-time target merging for Carry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from .config import CarryConfig
from .decision import TargetPlan, plan_signal_targets
from .minute_bars import FifteenMinuteBar
from .risk import PositionState, apply_chandelier


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
