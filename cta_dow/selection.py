"""Exact monthly eligibility policy for Dow shadow scores.

The Dow paper filters on realised profit, not on risk-adjusted profit: five
completed trades and a non-negative cumulative return. That is deliberately a
different gate from the Bollinger rule (which drops a product only when Sharpe
*and* Calmar are both negative), so the two policies stay in their own modules
rather than sharing a parameterised one that invites mixing them up.
"""

from __future__ import annotations

from collections.abc import Mapping

from common.commodity.selection import ProductScore

__all__ = ["MIN_TRADES", "eligible_products"]

MIN_TRADES = 5


def eligible_products(scores: Mapping[str, ProductScore]) -> tuple[str, ...]:
    """Return products with enough completed trades and no realised loss."""
    if not isinstance(scores, Mapping):
        raise ValueError("dow_selection: scores must be a mapping")

    selected: list[str] = []
    for product, score in sorted(scores.items()):
        if not isinstance(product, str) or not product:
            raise ValueError("dow_selection_product: expected nonempty string")
        if not isinstance(score, ProductScore):
            raise ValueError("dow_selection_score: expected ProductScore")
        if score.product != product:
            raise ValueError(
                "dow_selection_identity: mapping key must equal score.product"
            )
        if type(score.trade_count) is not int or score.trade_count < 0:
            raise ValueError("dow_selection_trade_count: expected nonnegative int")

        if score.trade_count < MIN_TRADES:
            continue
        if not (score.cumulative_return >= 0.0):
            continue
        selected.append(product)
    return tuple(selected)
