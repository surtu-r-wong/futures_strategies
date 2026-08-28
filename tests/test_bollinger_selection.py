from __future__ import annotations

from datetime import date

import math

import pytest

from common.commodity.selection import ProductScore
from cta_bollinger.selection import eligible_products


def score_factory(
    product: str,
    *,
    trade_count: int = 5,
    sharpe: float = 1.0,
    calmar: float = 1.0,
) -> ProductScore:
    return ProductScore(
        product=product,
        first_observation=date(2023, 1, 2),
        last_observation=date(2023, 12, 29),
        observations=252,
        trade_count=trade_count,
        cumulative_return=0.1,
        annual_return=0.1,
        annual_volatility=0.1,
        sharpe=sharpe,
        max_drawdown=0.05,
        calmar=calmar,
    )


def test_selector_applies_minimum_count_and_rejects_only_both_negative() -> None:
    scores = {
        "ZN": score_factory("ZN", sharpe=0.0, calmar=0.0),
        "RB": score_factory("RB", sharpe=-1.0, calmar=0.1),
        "CU": score_factory("CU", sharpe=-1.0, calmar=-0.1),
        "AL": score_factory("AL", trade_count=4, sharpe=2.0, calmar=2.0),
        "AU": score_factory("AU", sharpe=0.1, calmar=-1.0),
    }

    assert eligible_products(scores) == ("AU", "RB", "ZN")


def test_selector_preserves_nan_calmar_comparison_semantics() -> None:
    scores = {"RB": score_factory("RB", sharpe=-1.0, calmar=float("nan"))}

    assert eligible_products(scores) == ("RB",)


def test_selector_requires_score_identity_and_finite_policy_inputs() -> None:
    with pytest.raises(ValueError, match="identity"):
        eligible_products({"CU": score_factory("RB")})
    with pytest.raises(ValueError, match="sharpe.*finite"):
        eligible_products({"RB": score_factory("RB", sharpe=float("nan"))})
    with pytest.raises(ValueError, match="calmar"):
        eligible_products({"RB": score_factory("RB", calmar=math.inf)})
    with pytest.raises(ValueError, match="ProductScore"):
        eligible_products({"RB": object()})
