from dataclasses import dataclass
import math
from typing import Any


_POSITIVE_INTEGER_FIELDS = (
    "liquidity_window",
    "carry_window",
    "momentum_window",
    "atr_window",
    "vol_window",
    "stop_tranches",
    "prewarm_calendar_days",
)

_POSITIVE_NUMERIC_FIELDS = (
    "atr_risk_budget",
    "target_vol",
    "max_gross_leverage",
    "chandelier_atr_multiple",
)

_NONNEGATIVE_NUMERIC_FIELDS = ("liquidity_threshold", "cost_bps")


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class CarryConfig:
    liquidity_window: int = 120
    liquidity_threshold: float = 5_000_000_000.0
    carry_window: int = 10
    selection_fraction: float = 0.20
    momentum_window: int = 10
    atr_window: int = 20
    atr_risk_budget: float = 0.005
    vol_window: int = 252
    min_shadow_active_days: int = 126
    target_vol: float = 0.15
    max_gross_leverage: float = 4.0
    chandelier_atr_multiple: float = 2.5
    stop_tranches: int = 3
    cost_bps: float = 13.0
    prewarm_calendar_days: int = 730

    def __post_init__(self) -> None:
        for field_name in _POSITIVE_INTEGER_FIELDS:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")

        if (
            not _is_finite_number(self.selection_fraction)
            or not 0 < self.selection_fraction <= 0.5
        ):
            raise ValueError("selection_fraction must be in (0, 0.5]")

        if (
            isinstance(self.min_shadow_active_days, bool)
            or not isinstance(self.min_shadow_active_days, int)
            or not 1 <= self.min_shadow_active_days <= self.vol_window
        ):
            raise ValueError(
                "min_shadow_active_days must be in [1, vol_window]"
            )

        for field_name in _POSITIVE_NUMERIC_FIELDS:
            value = getattr(self, field_name)
            if not _is_finite_number(value) or value <= 0:
                raise ValueError(f"{field_name} must be finite and greater than 0")

        for field_name in _NONNEGATIVE_NUMERIC_FIELDS:
            value = getattr(self, field_name)
            if not _is_finite_number(value) or value < 0:
                raise ValueError(f"{field_name} must be finite and nonnegative")
