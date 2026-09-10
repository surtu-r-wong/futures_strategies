"""Execution-independent daily research and target planning for Carry."""

from dataclasses import dataclass
import math

import pandas as pd

from .config import CarryConfig
from .curve import CurveResult, build_curve
from .legreturns import attach_leg_returns
from .risk import (
    PositionState,
    apply_equal_weight_capital,
    compute_contract_atr,
    raw_target_weight,
    transition_signal,
)
from .signals import SignalResult, build_signals, rank_linear_weights


@dataclass(frozen=True)
class DailyResearch:
    curve_result: CurveResult
    contract_atr: pd.DataFrame
    signal_result: SignalResult


@dataclass(frozen=True)
class TargetPlan:
    states: dict[str, PositionState]
    raw_weights: dict[str, float]
    reasons: dict[str, str]


class SignalInputError(RuntimeError):
    def __init__(
        self,
        *,
        trade_date,
        product,
        contract,
        check,
        reason,
        value=None,
    ) -> None:
        self.trade_date = trade_date
        self.product = product
        self.contract = contract
        self.check = check
        self.reason = reason
        self.value = value
        super().__init__(
            f"{trade_date} {product} {contract} {check}: {reason}; value={value!r}"
        )


def _valid_positive(value) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0.0
    except (TypeError, ValueError, OverflowError):
        return False


def build_daily_research(
    prices: pd.DataFrame,
    config: CarryConfig,
) -> DailyResearch:
    curve_result = build_curve(prices, config)
    contract_atr = compute_contract_atr(prices, config)
    main_atr = contract_atr.loc[:, ["trade_date", "contract", "atr"]].rename(
        columns={"contract": "main_contract"}
    )
    curve_with_atr = curve_result.curve.merge(
        main_atr,
        on=["trade_date", "main_contract"],
        how="left",
        validate="one_to_one",
    )
    if float(getattr(config, "basis_momentum_weight", 0.0)) > 0.0:
        curve_with_atr = attach_leg_returns(prices, curve_with_atr)
    return DailyResearch(
        curve_result=curve_result,
        contract_atr=contract_atr,
        signal_result=build_signals(curve_with_atr, config),
    )


def plan_signal_targets(
    states: dict[str, PositionState],
    signal_rows: pd.DataFrame,
    config: CarryConfig,
    *,
    previous_states: dict[str, PositionState] | None = None,
    reason_hints: dict[str, str] | None = None,
) -> TargetPlan:
    """Apply signal transitions and raw risk sizing to prepared states."""
    if previous_states is None:
        previous_states = states
    if reason_hints is None:
        reason_hints = {}

    signals = {
        row.product: row
        for row in signal_rows.sort_values("product", kind="mergesort").itertuples(
            index=False
        )
    }
    products = sorted(set(states) | set(signals))
    next_states: dict[str, PositionState] = {}
    raw_weights: dict[str, float] = {}
    reasons: dict[str, str] = {}

    rank_linear = getattr(config, "weighting", "risk_budget") == "rank_linear"
    rank_weights: dict[str, float] = {}
    if rank_linear and not signal_rows.empty:
        ready = signal_rows.loc[signal_rows["input_ready"].astype(bool)]
        if "blend_weight" in ready.columns:
            # The basis-momentum leg is on, and signals already struck the
            # blend -- including the side each product trades.  Re-ranking
            # carry_ma here would size a product against the direction its
            # position was opened on.
            rank_weights = dict(zip(ready["product"], ready["blend_weight"]))
        elif len(ready) >= 5:
            ready = ready.sort_values(["carry_ma", "product"], kind="mergesort")
            rank_weights = dict(
                zip(ready["product"], rank_linear_weights(ready).to_numpy())
            )

    for product in products:
        before = previous_states.get(product, PositionState())
        transition_state = states.get(product, PositionState())
        signal = signals.get(product)
        direction = int(signal.effective_direction) if signal is not None else 0
        contract = signal.main_contract if direction != 0 else None
        after = transition_signal(transition_state, direction, contract, config)

        old_direction = (
            before.direction if before.direction != 0 else before.locked_direction
        )
        if old_direction != 0 and after.direction == -old_direction:
            reason = "direction_reversal"
        elif product in reason_hints:
            reason = reason_hints[product]
        elif before.direction != 0 and after.direction == 0:
            reason = "signal_exit"
        elif before.direction == 0 and after.direction != 0:
            reason = "entry"
        elif (
            before.direction == after.direction != 0
            and before.contract != after.contract
        ):
            reason = "roll"
        else:
            reason = "rebalance"

        next_states[product] = after
        reasons[product] = reason
        if after.direction != 0 and after.contract is not None:
            signal_date = getattr(signal, "trade_date", None)
            signal_atr = getattr(signal, "atr", None)
            if signal is None or not _valid_positive(signal_atr):
                raise SignalInputError(
                    trade_date=signal_date,
                    product=product,
                    contract=after.contract,
                    check="signal_atr",
                    reason="active target requires finite positive ATR",
                    value=signal_atr,
                )
            if rank_linear:
                # The centred rank is the whole size; the ATR budget, tranches
                # and close play no part.  strength still gates the filter.
                raw_weights[after.contract] = float(rank_weights[product]) * float(
                    signal.strength
                )
                continue
            try:
                raw_weights[after.contract] = raw_target_weight(
                    after.direction,
                    float(signal.strength),
                    float(signal.main_close),
                    float(signal.atr),
                    after.tranches_remaining,
                    config,
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise SignalInputError(
                    trade_date=signal_date,
                    product=product,
                    contract=after.contract,
                    check="target_sizing",
                    reason=str(exc),
                    value={
                        "strength": getattr(signal, "strength", None),
                        "close": getattr(signal, "main_close", None),
                        "atr": signal_atr,
                    },
                ) from exc

    return TargetPlan(
        states=next_states,
        raw_weights=apply_equal_weight_capital(raw_weights, config),
        reasons=reasons,
    )
