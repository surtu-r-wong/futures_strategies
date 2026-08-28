"""The Dow monthly eligibility policy: five trades and a non-negative record."""

from __future__ import annotations

from datetime import date

import pytest

from common.commodity.selection import ProductScore
from cta_dow.selection import eligible_products


def score_factory(**overrides) -> ProductScore:
    fields = {
        "product": "RB",
        "first_observation": date(2023, 1, 3),
        "last_observation": date(2023, 12, 29),
        "observations": 252,
        "trade_count": 9,
        "cumulative_return": 0.2,
        "annual_return": 0.2,
        "annual_volatility": 0.15,
        "sharpe": 1.3,
        "max_drawdown": 0.1,
        "calmar": 2.0,
    }
    fields.update(overrides)
    return ProductScore(**fields)


def test_selector_needs_five_trades_and_nonnegative_return() -> None:
    scores = {
        "RB": score_factory(product="RB", trade_count=5, cumulative_return=0.0),
        "CU": score_factory(product="CU", trade_count=5, cumulative_return=-0.001),
        "AL": score_factory(product="AL", trade_count=4, cumulative_return=1.0),
    }

    assert eligible_products(scores) == ("RB",)


def test_the_gate_is_cumulative_return_not_risk_adjusted() -> None:
    # The Dow paper filters on realised profit; a negative Sharpe with a
    # positive cumulative return stays in, unlike the Bollinger rule.
    scores = {
        "RB": score_factory(product="RB", cumulative_return=0.01, sharpe=-2.0, calmar=-3.0)
    }

    assert eligible_products(scores) == ("RB",)


def test_products_come_back_in_a_stable_order() -> None:
    scores = {
        product: score_factory(product=product)
        for product in ("TA", "AL", "RB", "CU")
    }

    assert eligible_products(scores) == ("AL", "CU", "RB", "TA")


def test_a_key_that_disagrees_with_its_score_is_rejected() -> None:
    with pytest.raises(ValueError, match="dow_selection_identity"):
        eligible_products({"RB": score_factory(product="CU")})


def test_a_non_mapping_is_rejected() -> None:
    with pytest.raises(ValueError, match="dow_selection"):
        eligible_products([score_factory()])
