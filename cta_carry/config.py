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
    "trend_confirm_days",
    "prewarm_calendar_days",
)

_POSITIVE_NUMERIC_FIELDS = (
    "atr_risk_budget",
    "target_vol",
    "max_gross_leverage",
    "chandelier_atr_multiple",
)

_NONNEGATIVE_NUMERIC_FIELDS = (
    "liquidity_threshold",
    "cost_bps",
    "trend_band_atr",
)

_SECONDARY_SELECTIONS = frozenset({"strictly_later", "second_by_oi"})


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
    # One-way cost in basis points of traded notional, covering commission and
    # slippage. Measured 2026-08-05 (design spec 3): realised commission on
    # commodity futures is 0.2-1.1 bps, and one minimum tick weighted by this
    # strategy's own holdings is 3.13 bps, so a market order crossing the
    # spread costs about 3.6 bps all in. 4.0 keeps a margin over that while
    # staying well under the 6.94 bps break-even.
    cost_bps: float = 4.0
    # Half-width, in contract ATRs, of the hysteresis band around the momentum
    # MA.  The trend state only flips once close clears the band, so a price
    # oscillating around its MA no longer flips the filter on and off.  0.0
    # disables the band and reproduces the original stateless MA comparison.
    trend_band_atr: float = 0.0
    # Whether to keep the branch that trades against the trend when volume and
    # open interest are both fading. Attribution over 2013-2026 puts that
    # branch at -0.102 across the three loss-making windows, against +0.220 for
    # the trend-aligned one. Not a tuning knob: it is here so the branch can be
    # measured without it. Default keeps it, so the baseline does not move.
    allow_trend_opposed: bool = True
    # Consecutive same-side closes required before the trend state flips.  1
    # flips on the first close and reproduces the original stateless rule.
    trend_confirm_days: int = 1
    # How the secondary contract is chosen.  "strictly_later" takes the
    # highest-OI contract delivering strictly after the main, which anchors the
    # Carry sign to the textbook near-vs-far definition.  "second_by_oi" takes
    # the second-highest-OI contract whatever its month, reproducing the
    # research report, which names a secondary but never defines it; on Chinese
    # exchanges that contract is often NEARER than the main.
    secondary_selection: str = "strictly_later"
    # Divide per-product ATR risk budgets by the number of qualifying products, so
    # the book does not grow just because more products qualify -- the report's
    # "equal capital across qualifying products".  Off by default, which sizes
    # each product on its own ATR budget and lets gross leverage scale with
    # breadth until the cap binds.
    equal_weight_capital: bool = False
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
            raise ValueError("min_shadow_active_days must be in [1, vol_window]")

        for field_name in _POSITIVE_NUMERIC_FIELDS:
            value = getattr(self, field_name)
            if not _is_finite_number(value) or value <= 0:
                raise ValueError(f"{field_name} must be finite and greater than 0")

        for field_name in _NONNEGATIVE_NUMERIC_FIELDS:
            value = getattr(self, field_name)
            if not _is_finite_number(value) or value < 0:
                raise ValueError(f"{field_name} must be finite and nonnegative")

        if type(self.equal_weight_capital) is not bool:
            raise ValueError("equal_weight_capital must be a bool")

        if self.secondary_selection not in _SECONDARY_SELECTIONS:
            raise ValueError(
                "secondary_selection must be one of "
                + ", ".join(sorted(_SECONDARY_SELECTIONS))
            )
