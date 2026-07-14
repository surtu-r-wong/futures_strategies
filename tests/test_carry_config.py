from dataclasses import FrozenInstanceError, is_dataclass
import math

import pytest

from cta_carry import CarryConfig


def test_carry_config_is_a_frozen_dataclass() -> None:
    config = CarryConfig()

    assert is_dataclass(CarryConfig)
    assert CarryConfig.__dataclass_params__.frozen is True
    with pytest.raises(FrozenInstanceError):
        config.liquidity_window = 60


def test_carry_config_defaults() -> None:
    assert CarryConfig() == CarryConfig(
        liquidity_window=120,
        liquidity_threshold=5_000_000_000.0,
        carry_window=10,
        selection_fraction=0.20,
        momentum_window=10,
        atr_window=20,
        atr_risk_budget=0.005,
        vol_window=252,
        min_shadow_active_days=126,
        target_vol=0.15,
        max_gross_leverage=4.0,
        chandelier_atr_multiple=2.5,
        stop_tranches=3,
        cost_bps=13.0,
        prewarm_calendar_days=730,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("liquidity_window", 0),
        ("selection_fraction", 0),
        ("selection_fraction", 0.51),
        ("target_vol", 0),
        ("cost_bps", -1),
    ],
)
def test_carry_config_rejects_required_invalid_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match=field):
        CarryConfig(**{field: value})


def test_carry_config_rejects_shadow_history_longer_than_vol_window() -> None:
    with pytest.raises(ValueError, match="min_shadow_active_days"):
        CarryConfig(vol_window=10, min_shadow_active_days=11)


@pytest.mark.parametrize(
    "field",
    [
        "liquidity_window",
        "carry_window",
        "momentum_window",
        "atr_window",
        "vol_window",
        "stop_tranches",
        "prewarm_calendar_days",
    ],
)
@pytest.mark.parametrize("value", [True, 1.5, 0, -1])
def test_positive_integer_fields_require_positive_non_bool_integers(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match=field):
        CarryConfig(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "atr_risk_budget",
        "target_vol",
        "max_gross_leverage",
        "chandelier_atr_multiple",
    ],
)
@pytest.mark.parametrize("value", [0, -1, math.inf, -math.inf, math.nan])
def test_strictly_positive_numeric_fields_require_finite_positive_values(
    field: str, value: float
) -> None:
    with pytest.raises(ValueError, match=field):
        CarryConfig(**{field: value})


@pytest.mark.parametrize("field", ["liquidity_threshold", "cost_bps"])
@pytest.mark.parametrize("value", [-1, math.inf, -math.inf, math.nan])
def test_nonnegative_numeric_fields_require_finite_nonnegative_values(
    field: str, value: float
) -> None:
    with pytest.raises(ValueError, match=field):
        CarryConfig(**{field: value})


@pytest.mark.parametrize("value", [0, -1, 253])
def test_min_shadow_active_days_must_be_within_vol_window(value: int) -> None:
    with pytest.raises(ValueError, match="min_shadow_active_days"):
        CarryConfig(min_shadow_active_days=value)
