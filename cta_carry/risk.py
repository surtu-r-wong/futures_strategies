"""Pure risk sizing, volatility, and stop-state helpers for Carry."""
from collections import deque
from dataclasses import dataclass, replace
import math

import numpy as np
import pandas as pd


_ATR_COLUMNS = ("trade_date", "contract", "tr", "atr")


def _is_finite_number(value) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return False
    try:
        return bool(math.isfinite(value))
    except (TypeError, ValueError):
        return False


def compute_contract_atr(prices: pd.DataFrame, config) -> pd.DataFrame:
    """Compute true range and a complete simple ATR per contract."""
    if prices.empty:
        return pd.DataFrame(columns=_ATR_COLUMNS)

    ordered = prices.sort_values(
        ["contract", "trade_date"],
        kind="mergesort",
    ).copy()
    previous_close = ordered.groupby(
        "contract",
        sort=False,
    )["close"].shift(1)
    observation_number = ordered.groupby(
        "contract",
        sort=False,
    ).cumcount()
    current_is_valid = np.isfinite(
        ordered[["high", "low", "close"]]
    ).fillna(False).all(axis=1)
    previous_is_valid = observation_number.eq(0) | np.isfinite(
        previous_close
    ).fillna(False)
    true_range_is_valid = current_is_valid & previous_is_valid
    true_range_components = pd.concat(
        [
            ordered["high"] - ordered["low"],
            (ordered["high"] - previous_close).abs(),
            (ordered["low"] - previous_close).abs(),
        ],
        axis=1,
    )
    ordered["tr"] = true_range_components.max(axis=1).where(
        true_range_is_valid
    )
    ordered["atr"] = ordered.groupby(
        "contract",
        sort=False,
    )["tr"].transform(
        lambda values: values.rolling(
            config.atr_window,
            min_periods=config.atr_window,
        ).mean()
    )
    return ordered.loc[:, list(_ATR_COLUMNS)].sort_values(
        ["trade_date", "contract"],
        kind="mergesort",
    ).reset_index(drop=True)


def raw_target_weight(
    direction,
    strength,
    close,
    atr,
    tranches_remaining,
    config,
) -> float:
    """Compute the unscaled risk-budget target weight."""
    if isinstance(direction, (bool, np.bool_)) or direction not in (-1, 0, 1):
        raise ValueError("direction must be -1, 0, or 1")
    if not _is_finite_number(strength) or not 0.0 <= strength <= 1.0:
        raise ValueError("strength must be finite and in [0, 1]")
    if not _is_finite_number(close) or close <= 0.0:
        raise ValueError("close must be finite and positive")
    if not _is_finite_number(atr) or atr <= 0.0:
        raise ValueError("atr must be finite and positive")
    if (
        isinstance(tranches_remaining, bool)
        or not isinstance(tranches_remaining, int)
        or not 0 <= tranches_remaining <= config.stop_tranches
    ):
        raise ValueError(
            "tranches_remaining must be an integer in [0, stop_tranches]"
        )

    try:
        raw_weight = (
            direction
            * strength
            * config.atr_risk_budget
            * close
            / atr
            * tranches_remaining
            / config.stop_tranches
        )
    except (FloatingPointError, OverflowError) as exc:
        raise ValueError("raw target weight must be finite") from exc
    if not _is_finite_number(raw_weight):
        raise ValueError("raw target weight must be finite")
    return float(raw_weight)


def scale_weights(raw_weights, vol_scale, config) -> dict:
    """Apply volatility scaling and a proportional gross leverage cap."""
    if not _is_finite_number(vol_scale) or vol_scale <= 0.0:
        raise ValueError("vol_scale must be finite and positive")
    scale = float(vol_scale)

    finite_weights = {}
    for contract, raw_weight in raw_weights.items():
        if not _is_finite_number(raw_weight):
            raise ValueError("raw weight must be finite")
        weight = float(raw_weight)
        if not math.isfinite(weight):
            raise ValueError("raw weight must be finite")
        if weight != 0.0:
            finite_weights[contract] = weight
    if not finite_weights:
        return {}

    max_abs = max(abs(weight) for weight in finite_weights.values())
    normalized_gross = math.fsum(
        abs(weight) / max_abs
        for weight in finite_weights.values()
    )
    gross_cap = float(config.max_gross_leverage)
    max_uncapped_scale = gross_cap / normalized_gross / max_abs
    if scale > max_uncapped_scale:
        capped_normalized_gross = gross_cap / normalized_gross
        scaled = {
            contract: weight / max_abs * capped_normalized_gross
            for contract, weight in finite_weights.items()
        }
    else:
        scaled = {
            contract: weight * scale
            for contract, weight in finite_weights.items()
        }

    if not all(math.isfinite(weight) for weight in scaled.values()):
        raise ValueError("scaled weight must be finite")
    gross = math.fsum(abs(weight) for weight in scaled.values())
    if not math.isfinite(gross):
        raise ValueError("scaled gross leverage must be finite")
    if gross > gross_cap:
        adjustment = gross_cap / gross
        scaled = {
            contract: weight * adjustment
            for contract, weight in scaled.items()
        }
        gross = math.fsum(abs(weight) for weight in scaled.values())
    if gross > gross_cap:
        adjustment = math.nextafter(gross_cap, 0.0) / gross
        scaled = {
            contract: weight * adjustment
            for contract, weight in scaled.items()
        }
        gross = math.fsum(abs(weight) for weight in scaled.values())
    if (
        not all(math.isfinite(weight) for weight in scaled.values())
        or not math.isfinite(gross)
        or gross > gross_cap
    ):
        raise ValueError("scaled weights must satisfy the gross leverage cap")
    return {
        contract: weight
        for contract, weight in scaled.items()
        if weight != 0.0
    }


@dataclass(frozen=True)
class VolEstimate:
    observations: int
    active_days: int
    realized_vol: float
    vol_scale: float
    ready: bool


class ShadowVolWindow:
    """Rolling shadow-return window used for live volatility scaling."""

    def __init__(self, config) -> None:
        self._config = config
        self._returns = deque(maxlen=config.vol_window)
        self._active = deque(maxlen=config.vol_window)

    def append(self, net_return, active) -> None:
        if not _is_finite_number(net_return):
            raise ValueError("net_return must be finite")
        if type(active) is not bool:
            raise ValueError("active must be a bool")
        self._returns.append(float(net_return))
        self._active.append(active)

    def estimate(self) -> VolEstimate:
        observations = len(self._returns)
        active_days = sum(self._active)
        if observations:
            realized_vol = float(
                np.std(self._returns, ddof=0) * np.sqrt(252.0)
            )
        else:
            realized_vol = float("nan")

        ready = (
            observations == self._config.vol_window
            and active_days >= self._config.min_shadow_active_days
            and math.isfinite(realized_vol)
            and realized_vol > 0.0
        )
        vol_scale = (
            self._config.target_vol / realized_vol
            if ready
            else float("nan")
        )
        return VolEstimate(
            observations=observations,
            active_days=active_days,
            realized_vol=realized_vol,
            vol_scale=vol_scale,
            ready=ready,
        )


@dataclass(frozen=True)
class PositionState:
    direction: int = 0
    contract: str | None = None
    tranches_remaining: int = 0
    highest_high: float | None = None
    lowest_low: float | None = None
    locked_direction: int = 0


def transition_signal(
    state: PositionState,
    direction,
    contract,
    config,
) -> PositionState:
    """Transition a position on a new directional signal or contract roll."""
    if isinstance(direction, (bool, np.bool_)) or direction not in (-1, 0, 1):
        raise ValueError("direction must be -1, 0, or 1")
    if direction == 0:
        return PositionState()
    if not isinstance(contract, str) or not contract:
        raise ValueError("contract is required for a nonzero direction")

    if state.direction == 0 and state.locked_direction == direction:
        return state
    if state.direction == direction:
        if state.contract == contract:
            return state
        return PositionState(
            direction=direction,
            contract=contract,
            tranches_remaining=state.tranches_remaining,
        )
    return PositionState(
        direction=direction,
        contract=contract,
        tranches_remaining=config.stop_tranches,
    )


def _validate_stop_inputs(high, low, close, atr) -> None:
    for name, value in (
        ("high", high),
        ("low", low),
        ("close", close),
    ):
        if not _is_finite_number(value):
            raise ValueError(f"{name} must be finite")
    if not _is_finite_number(atr) or atr <= 0.0:
        raise ValueError("atr must be finite and positive")


def apply_chandelier(
    state: PositionState,
    high,
    low,
    close,
    atr,
    config,
) -> tuple[PositionState, bool]:
    """Apply at most one chandelier stop tranche to an active position."""
    if state.direction == 0:
        return state, False
    if state.direction not in (-1, 1):
        raise ValueError("state direction must be -1, 0, or 1")
    _validate_stop_inputs(high, low, close, atr)

    triggered = False
    if state.direction == 1:
        highest_high = (
            high
            if state.highest_high is None
            else max(state.highest_high, high)
        )
        triggered = (
            close
            < highest_high
            - config.chandelier_atr_multiple * atr
        )
        updated = replace(state, highest_high=highest_high)
    else:
        lowest_low = (
            low
            if state.lowest_low is None
            else min(state.lowest_low, low)
        )
        triggered = (
            close
            > lowest_low
            + config.chandelier_atr_multiple * atr
        )
        updated = replace(state, lowest_low=lowest_low)

    if not triggered:
        return updated, False

    tranches_remaining = state.tranches_remaining - 1
    if tranches_remaining <= 0:
        return PositionState(locked_direction=state.direction), True
    return replace(updated, tranches_remaining=tranches_remaining), True
