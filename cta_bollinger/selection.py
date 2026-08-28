"""Exact monthly eligibility policy for Bollinger shadow scores."""

from __future__ import annotations

from collections.abc import Mapping
import math

from common.commodity.selection import ProductScore

__all__ = ["eligible_products"]


def eligible_products(scores: Mapping[str, ProductScore]) -> tuple[str, ...]:
    """Return products passing the paper's count and joint-risk gates.

    A ``NaN`` Calmar is intentionally retained in the ordinary Python
    comparison below: it is not less than zero, so it cannot form the joint
    negative condition with Sharpe.
    """
    if not isinstance(scores, Mapping):
        raise ValueError("bollinger_selection: scores must be a mapping")

    selected: list[str] = []
    for product, score in sorted(scores.items()):
        if not isinstance(product, str) or not product:
            raise ValueError("bollinger_selection_product: expected nonempty string")
        if not isinstance(score, ProductScore):
            raise ValueError("bollinger_selection_score: expected ProductScore")
        if score.product != product:
            raise ValueError(
                "bollinger_selection_identity: mapping key must equal score.product"
            )
        if type(score.trade_count) is not int or score.trade_count < 0:
            raise ValueError(
                "bollinger_selection_trade_count: expected nonnegative int"
            )
        if not math.isfinite(score.sharpe):
            raise ValueError("bollinger_selection_sharpe: expected finite value")
        if math.isinf(score.calmar):
            raise ValueError("bollinger_selection_calmar: infinity is invalid")

        if score.trade_count < 5:
            continue
        if score.sharpe < 0.0 and score.calmar < 0.0:
            continue
        selected.append(product)
    return tuple(selected)
