from dataclasses import FrozenInstanceError
from datetime import date

import numpy as np
import pandas as pd
import pytest

from cta_carry.config import CarryConfig
from cta_carry.risk import (
    PositionState,
    ShadowVolWindow,
    VolEstimate,
    apply_chandelier,
    compute_contract_atr,
    raw_target_weight,
    scale_weights,
    transition_signal,
)


def _config(**overrides):
    values = {
        "atr_window": 2,
        "atr_risk_budget": 0.005,
        "vol_window": 4,
        "min_shadow_active_days": 2,
        "target_vol": 0.15,
        "max_gross_leverage": 4.0,
        "chandelier_atr_multiple": 2.5,
        "stop_tranches": 3,
    }
    values.update(overrides)
    return CarryConfig(**values)


def test_contract_atr_uses_previous_close_from_the_same_contract() -> None:
    first = date(2024, 1, 2)
    second = date(2024, 1, 3)
    prices = pd.DataFrame(
        [
            {
                "trade_date": second,
                "contract": "B",
                "high": 202.0,
                "low": 199.0,
                "close": 201.0,
            },
            {
                "trade_date": first,
                "contract": "A",
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
            },
            {
                "trade_date": first,
                "contract": "B",
                "high": 201.0,
                "low": 199.0,
                "close": 200.0,
            },
            {
                "trade_date": second,
                "contract": "A",
                "high": 103.0,
                "low": 100.0,
                "close": 102.0,
            },
        ]
    )

    result = compute_contract_atr(prices, _config())

    assert result.columns.tolist() == ["trade_date", "contract", "tr", "atr"]
    assert result[["trade_date", "contract"]].values.tolist() == [
        [first, "A"],
        [first, "B"],
        [second, "A"],
        [second, "B"],
    ]
    assert result["tr"].tolist() == [2.0, 2.0, 3.0, 3.0]
    assert result["atr"].isna().tolist() == [True, True, False, False]
    assert result.loc[result["trade_date"] == second, "atr"].tolist() == [
        2.5,
        2.5,
    ]


def test_contract_atr_empty_input_preserves_stable_columns() -> None:
    result = compute_contract_atr(pd.DataFrame(), _config())

    assert result.empty
    assert result.columns.tolist() == ["trade_date", "contract", "tr", "atr"]


def test_raw_target_weight_matches_risk_budget_formula() -> None:
    result = raw_target_weight(
        direction=-1,
        strength=0.5,
        close=100.0,
        atr=2.0,
        tranches_remaining=2,
        config=_config(),
    )

    assert result == pytest.approx(-0.5 * 0.005 * 100.0 / 2.0 * 2.0 / 3.0)
    assert raw_target_weight(0, 1.0, 100.0, 2.0, 3, _config()) == 0.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"direction": 2}, "direction"),
        ({"strength": -0.1}, "strength"),
        ({"strength": 1.1}, "strength"),
        ({"strength": float("nan")}, "strength"),
        ({"close": 0.0}, "close"),
        ({"close": float("inf")}, "close"),
        ({"atr": 0.0}, "atr"),
        ({"atr": float("nan")}, "atr"),
        ({"tranches_remaining": -1}, "tranches_remaining"),
        ({"tranches_remaining": 4}, "tranches_remaining"),
        ({"tranches_remaining": 1.5}, "tranches_remaining"),
        ({"tranches_remaining": True}, "tranches_remaining"),
    ],
)
def test_raw_target_weight_rejects_invalid_inputs(kwargs, message) -> None:
    values = {
        "direction": 1,
        "strength": 0.5,
        "close": 100.0,
        "atr": 2.0,
        "tranches_remaining": 2,
        "config": _config(),
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        raw_target_weight(**values)


def test_scale_weights_preserves_signs_at_exact_gross_cap_and_drops_zero() -> None:
    scaled = scale_weights(
        {"long": 0.03, "short": -0.01, "flat": 0.0},
        vol_scale=100.0,
        config=_config(),
    )

    assert set(scaled) == {"long", "short"}
    assert scaled["long"] == pytest.approx(3.0)
    assert scaled["short"] == pytest.approx(-1.0)
    assert sum(abs(weight) for weight in scaled.values()) == pytest.approx(4.0)


def test_scale_weights_clips_all_positions_proportionally() -> None:
    scaled = scale_weights(
        {"long": 0.04, "short": -0.02},
        vol_scale=100.0,
        config=_config(),
    )

    assert scaled["long"] == pytest.approx(8.0 / 3.0)
    assert scaled["short"] == pytest.approx(-4.0 / 3.0)
    assert sum(abs(weight) for weight in scaled.values()) == pytest.approx(4.0)


@pytest.mark.parametrize("vol_scale", [0.0, -1.0, float("nan"), float("inf")])
def test_scale_weights_rejects_invalid_vol_scale(vol_scale) -> None:
    with pytest.raises(ValueError, match="vol_scale"):
        scale_weights({"RB": 0.1}, vol_scale, _config())


def test_scale_weights_rejects_nonfinite_raw_weight() -> None:
    with pytest.raises(ValueError, match="raw weight"):
        scale_weights({"RB": float("nan")}, 1.0, _config())


def test_shadow_vol_window_estimates_ready_volatility_and_scale() -> None:
    window = ShadowVolWindow(_config())
    for net_return, active in (
        (0.0, False),
        (0.0, False),
        (0.01, True),
        (-0.01, True),
    ):
        window.append(net_return, active)

    estimate = window.estimate()
    expected_vol = np.std(
        [0.0, 0.0, 0.01, -0.01], ddof=0
    ) * np.sqrt(252.0)

    assert isinstance(estimate, VolEstimate)
    assert estimate.observations == 4
    assert estimate.active_days == 2
    assert estimate.realized_vol == pytest.approx(expected_vol)
    assert estimate.vol_scale == pytest.approx(0.15 / expected_vol)
    assert estimate.ready is True
    with pytest.raises(FrozenInstanceError):
        estimate.ready = False


def test_shadow_vol_window_is_not_ready_when_realized_vol_is_zero() -> None:
    window = ShadowVolWindow(_config())
    for _ in range(4):
        window.append(0.0, True)

    estimate = window.estimate()

    assert estimate.observations == 4
    assert estimate.active_days == 4
    assert estimate.realized_vol == 0.0
    assert np.isnan(estimate.vol_scale)
    assert estimate.ready is False


def test_shadow_vol_window_rolls_both_deques_to_configured_maxlen() -> None:
    window = ShadowVolWindow(_config())
    window.append(0.1, True)
    for _ in range(4):
        window.append(0.0, False)

    estimate = window.estimate()

    assert estimate.observations == 4
    assert estimate.active_days == 0
    assert estimate.realized_vol == 0.0


@pytest.mark.parametrize("net_return", [float("nan"), float("inf")])
def test_shadow_vol_window_rejects_nonfinite_returns(net_return) -> None:
    window = ShadowVolWindow(_config())

    with pytest.raises(ValueError, match="net_return"):
        window.append(net_return, True)

    assert window.estimate().observations == 0


def test_shadow_vol_window_rejects_nonboolean_active_flag() -> None:
    with pytest.raises(ValueError, match="active"):
        ShadowVolWindow(_config()).append(0.01, 1)


def test_long_chandelier_stops_one_tranche_per_day_then_locks_direction() -> None:
    config = _config()
    state = transition_signal(PositionState(), 1, "RB2405.SHF", config)

    state, triggered = apply_chandelier(
        state, 110.0, 100.0, 107.0, 1.0, config
    )
    assert triggered is True
    assert state.tranches_remaining == 2
    state, triggered = apply_chandelier(
        state, 112.0, 101.0, 109.0, 1.0, config
    )
    assert triggered is True
    assert state.tranches_remaining == 1
    state, triggered = apply_chandelier(
        state, 114.0, 102.0, 111.0, 1.0, config
    )

    assert triggered is True
    assert state == PositionState(locked_direction=1)
    with pytest.raises(FrozenInstanceError):
        state.direction = 1
    assert transition_signal(state, 1, "RB2405.SHF", config) == state
    assert transition_signal(state, 0, None, config) == PositionState()
    reversed_state = transition_signal(state, -1, "RB2405.SHF", config)
    assert reversed_state.direction == -1
    assert reversed_state.tranches_remaining == config.stop_tranches


def test_chandelier_threshold_equality_does_not_trigger() -> None:
    config = _config()
    state = transition_signal(PositionState(), 1, "RB2405.SHF", config)

    updated, triggered = apply_chandelier(
        state, 110.0, 100.0, 107.5, 1.0, config
    )

    assert triggered is False
    assert updated.tranches_remaining == 3
    assert updated.highest_high == 110.0


def test_short_chandelier_updates_lowest_low_and_triggers_symmetrically() -> None:
    config = _config()
    state = transition_signal(PositionState(), -1, "RB2405.SHF", config)

    updated, triggered = apply_chandelier(
        state, 100.0, 90.0, 93.0, 1.0, config
    )

    assert triggered is True
    assert updated.direction == -1
    assert updated.tranches_remaining == 2
    assert updated.lowest_low == 90.0


def test_transition_roll_preserves_tranches_and_resets_extremes() -> None:
    config = _config()
    state = PositionState(
        direction=1,
        contract="RB2405.SHF",
        tranches_remaining=2,
        highest_high=110.0,
        lowest_low=90.0,
        locked_direction=1,
    )

    rolled = transition_signal(state, 1, "RB2410.SHF", config)

    assert rolled == PositionState(
        direction=1,
        contract="RB2410.SHF",
        tranches_remaining=2,
    )


def test_transition_same_contract_is_unchanged_and_reverse_restarts_full() -> None:
    config = _config()
    state = PositionState(
        direction=1,
        contract="RB2405.SHF",
        tranches_remaining=1,
        highest_high=110.0,
    )

    assert transition_signal(state, 1, "RB2405.SHF", config) is state
    reversed_state = transition_signal(state, -1, "RB2405.SHF", config)
    assert reversed_state == PositionState(
        direction=-1,
        contract="RB2405.SHF",
        tranches_remaining=3,
    )


@pytest.mark.parametrize("direction", [-2, 2])
def test_transition_rejects_invalid_direction(direction) -> None:
    with pytest.raises(ValueError, match="direction"):
        transition_signal(PositionState(), direction, "RB2405.SHF", _config())


def test_transition_requires_contract_for_nonzero_signal() -> None:
    with pytest.raises(ValueError, match="contract"):
        transition_signal(PositionState(), 1, None, _config())


def test_flat_chandelier_is_a_noop() -> None:
    state = PositionState()

    updated, triggered = apply_chandelier(
        state,
        float("nan"),
        float("nan"),
        float("nan"),
        float("nan"),
        _config(),
    )

    assert updated is state
    assert triggered is False


@pytest.mark.parametrize(
    "values",
    [
        (float("nan"), 1.0, 1.0, 1.0),
        (1.0, float("inf"), 1.0, 1.0),
        (1.0, 1.0, float("nan"), 1.0),
        (1.0, 1.0, 1.0, 0.0),
    ],
)
def test_active_chandelier_rejects_invalid_market_inputs(values) -> None:
    state = transition_signal(PositionState(), 1, "RB2405.SHF", _config())

    with pytest.raises(ValueError):
        apply_chandelier(state, *values, _config())


def test_contract_atr_propagates_invalid_ohlc_and_previous_close() -> None:
    dates = pd.bdate_range("2024-01-02", periods=6).date.tolist()
    prices = pd.DataFrame(
        [
            {
                "trade_date": dates[0],
                "contract": "A",
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
            },
            {
                "trade_date": dates[1],
                "contract": "A",
                "high": float("nan"),
                "low": 100.0,
                "close": 101.0,
            },
            {
                "trade_date": dates[2],
                "contract": "A",
                "high": 103.0,
                "low": 101.0,
                "close": float("nan"),
            },
            {
                "trade_date": dates[3],
                "contract": "A",
                "high": 104.0,
                "low": 102.0,
                "close": 103.0,
            },
            {
                "trade_date": dates[4],
                "contract": "A",
                "high": 105.0,
                "low": 103.0,
                "close": 104.0,
            },
            {
                "trade_date": dates[5],
                "contract": "A",
                "high": 106.0,
                "low": 104.0,
                "close": 105.0,
            },
        ]
    )

    result = compute_contract_atr(prices, _config())

    assert result.loc[0, "tr"] == 2.0
    assert result["tr"].isna().tolist() == [
        False,
        True,
        True,
        True,
        False,
        False,
    ]
    assert result["atr"].isna().tolist() == [
        True,
        True,
        True,
        True,
        True,
        False,
    ]
    assert result.loc[5, "atr"] == 2.0


def test_scale_weights_caps_extreme_opposite_weights_without_overflow() -> None:
    scaled = scale_weights(
        {"A": 1e308, "B": -1e308},
        vol_scale=1.0,
        config=_config(max_gross_leverage=4.0),
    )

    assert scaled["A"] == pytest.approx(2.0)
    assert scaled["B"] == pytest.approx(-2.0)
    assert all(np.isfinite(weight) for weight in scaled.values())
    assert sum(abs(weight) for weight in scaled.values()) == pytest.approx(4.0)


def test_raw_target_weight_rejects_nonfinite_formula_result() -> None:
    with pytest.raises(ValueError, match="raw target weight"):
        raw_target_weight(
            direction=1,
            strength=1.0,
            close=1e308,
            atr=1e-308,
            tranches_remaining=3,
            config=_config(),
        )


def test_risk_direction_inputs_reject_numpy_booleans() -> None:
    with pytest.raises(ValueError, match="direction"):
        raw_target_weight(
            direction=np.bool_(True),
            strength=1.0,
            close=100.0,
            atr=2.0,
            tranches_remaining=3,
            config=_config(),
        )
    with pytest.raises(ValueError, match="direction"):
        transition_signal(
            PositionState(),
            np.bool_(True),
            "RB2405.SHF",
            _config(),
        )


def test_contract_atr_rejects_nullable_ohlc_and_previous_close() -> None:
    dates = pd.bdate_range("2024-02-01", periods=6).date.tolist()
    prices = pd.DataFrame(
        {
            "trade_date": dates,
            "contract": ["A"] * 6,
            "high": pd.Series(
                [101.0, pd.NA, 103.0, 104.0, 105.0, 106.0],
                dtype="Float64",
            ),
            "low": pd.Series(
                [99.0, 100.0, 101.0, 102.0, 103.0, 104.0],
                dtype="Float64",
            ),
            "close": pd.Series(
                [100.0, 101.0, pd.NA, 103.0, 104.0, 105.0],
                dtype="Float64",
            ),
        }
    )

    result = compute_contract_atr(prices, _config())

    assert result.loc[0, "tr"] == 2.0
    assert result["tr"].isna().tolist() == [
        False,
        True,
        True,
        True,
        False,
        False,
    ]
    assert result["atr"].isna().tolist() == [
        True,
        True,
        True,
        True,
        True,
        False,
    ]
    assert result.loc[5, "atr"] == 2.0
