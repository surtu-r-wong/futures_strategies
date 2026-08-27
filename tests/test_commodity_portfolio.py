from __future__ import annotations

import math

import pytest

from common.commodity.portfolio import active_weights, fixed_universe_weights


def test_fixed_universe_keeps_inactive_capital_in_cash() -> None:
    assert fixed_universe_weights({"RB": 1, "CU": 0}, universe=("RB", "CU")) == {
        "RB": 0.5,
        "CU": 0.0,
    }


def test_fixed_universe_preserves_universe_order_and_defaults_missing_to_cash() -> None:
    result = fixed_universe_weights({"RB": -1}, universe=("CU", "RB", "AL"))

    assert list(result) == ["CU", "RB", "AL"]
    assert result == {"CU": 0.0, "RB": -1.0 / 3.0, "AL": 0.0}


def test_fixed_universe_rejects_outsiders() -> None:
    with pytest.raises(ValueError, match=r"allocation_outside_universe.*ZN"):
        fixed_universe_weights({"RB": 1, "ZN": -1}, universe=("RB", "CU"))


def test_fixed_universe_accepts_an_empty_universe() -> None:
    assert fixed_universe_weights({}, universe=()) == {}


def test_fixed_universe_rejects_duplicate_products() -> None:
    with pytest.raises(ValueError, match="universe.*duplicate"):
        fixed_universe_weights({"RB": 1}, universe=("RB", "RB"))


def test_active_weights_reallocate_only_across_positions() -> None:
    assert active_weights({"RB": 1, "CU": 0, "AL": -1}) == {
        "RB": 0.5,
        "CU": 0.0,
        "AL": -0.5,
    }


def test_active_weights_preserve_direction_order() -> None:
    result = active_weights({"CU": 0, "AL": -1, "RB": 1})

    assert list(result) == ["CU", "AL", "RB"]


def test_active_weights_leave_everything_in_cash_when_no_position_is_active() -> None:
    assert active_weights({"RB": 0, "CU": 0}) == {"RB": 0.0, "CU": 0.0}
    assert active_weights({}) == {}


@pytest.mark.parametrize("side", [2, -2, 0.5, True, math.nan, math.inf, "1"])
@pytest.mark.parametrize("allocator", ["fixed", "active"])
def test_allocators_reject_non_direction_values(side: object, allocator: str) -> None:
    with pytest.raises(ValueError, match="direction.*-1, 0, or 1"):
        if allocator == "fixed":
            fixed_universe_weights({"RB": side}, universe=("RB",))
        else:
            active_weights({"RB": side})


@pytest.mark.parametrize("product", ["", 1, None])
def test_allocators_require_nonempty_product_names(product: object) -> None:
    with pytest.raises(ValueError, match="product.*nonempty string"):
        active_weights({product: 1})
