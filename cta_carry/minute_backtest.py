"""Intraday stop decisions and close-time target merging for Carry."""

from __future__ import annotations

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


def merge_close_plan(
    post_stop_states: dict[str, PositionState],
    signal_rows: pd.DataFrame,
    config: CarryConfig,
) -> TargetPlan:
    """Merge a final-bar stop and the close signal into one net target plan."""
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
            continue
        signal_direction = int(signal.effective_direction)
        if signal_direction == 0 and state.locked_direction != 0:
            reason_hints[product] = "signal_exit"
            continue
        if state.direction != 0:
            same_cycle = (
                signal_direction == state.direction
                and signal.main_contract == state.contract
            )
            stage = config.stop_tranches - state.tranches_remaining
        else:
            same_cycle = (
                state.locked_direction != 0
                and signal_direction == state.locked_direction
            )
            stage = config.stop_tranches
        if same_cycle and stage > 0:
            reason_hints[product] = f"stop_{stage}"

    return plan_signal_targets(
        post_stop_states,
        signal_rows,
        config,
        previous_states=post_stop_states,
        reason_hints=reason_hints,
    )
