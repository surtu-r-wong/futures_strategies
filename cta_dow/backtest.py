"""The selected Dow portfolio: active-position capital, 15% target vol.

Only the policy lives here. Capital is split between the products that are
*currently holding*, not across the selected universe -- a selected product
with no signal does not occupy a share of the denominator (D7), and the
multiplier targets 15%. That denominator moves whenever any product enters or
leaves, so every other holder's target moves with it; the shared runner
resizes each of them at its own next fill window rather than at the price of
the product that caused the change.

The event loop, the parallel pre-volatility ledger, rolls, and the month
boundary are shared with the Bollinger replication in
``common.commodity.backtest``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from common.commodity.backtest import (
    COST_BPS,
    POSITION_COLUMNS,
    SELECTION_OBSERVATIONS,
    VOL_OBSERVATIONS,
    Allocation,
    BacktestResult,
    StrategySpec,
    run_portfolio_backtest,
)
from common.commodity.portfolio import active_weights, fixed_universe_weights
from common.commodity.selection import ProductScore
from cta_dow.selection import MIN_TRADES, eligible_products

__all__ = ["ALLOCATIONS", "BacktestResult", "TARGET_ANNUAL_VOL", "run_backtest"]


#: 研报 §6.6：组合年化波动目标 15%。
TARGET_ANNUAL_VOL = 0.15


def _allocate(selected: tuple[str, ...], directions: Mapping[str, float]) -> Allocation:
    active = [product for product, side in directions.items() if side]
    unit = 1.0 / len(active) if active else 0.0
    return Allocation(
        signed=active_weights(directions),
        sleeve={product: unit for product in active},
        active_products=len(active),
    )


def _allocate_selected(
    selected: tuple[str, ...], directions: Mapping[str, float]
) -> Allocation:
    """The other reading of D7: the denominator is the selected universe.

    The paper's line is "资金在满足开仓条件的品种间等权分配"; the Bollinger paper
    says "经筛选后品种等权分配资金" for the same step, and both papers report
    an average leverage near two with the same target-vol machinery. Read that
    way, a selected product with no signal keeps its share in cash and nobody
    is resized when someone else enters or leaves. Declared as a sensitivity;
    the registered default above is unchanged.
    """
    unit = 1.0 / len(selected) if selected else 0.0
    return Allocation(
        signed=fixed_universe_weights(directions, universe=selected),
        sleeve={product: unit for product in selected},
        active_products=sum(1 for side in directions.values() if side),
    )


ALLOCATIONS = ("active", "selected")


def _rejection(score: ProductScore) -> str | None:
    if score.trade_count < MIN_TRADES:
        return "too_few_trades"
    if not (score.cumulative_return >= 0.0):
        return "negative_cumulative_return"
    return None


def run_backtest(
    *,
    bundle: Any,
    shadows: Mapping[str, Any],
    target_vol: float = TARGET_ANNUAL_VOL,
    cost_bps: float = COST_BPS,
    realized_vol_min_observations: int = VOL_OBSERVATIONS,
    selection_observations: int = SELECTION_OBSERVATIONS,
    allocation: str = "active",
) -> BacktestResult:
    """Run the selected Dow portfolio over a bundle and its shadows."""
    if allocation not in ALLOCATIONS:
        raise ValueError(
            f"dow_backtest_allocation: expected one of {ALLOCATIONS}; got {allocation!r}"
        )
    selected_universe = allocation == "selected"
    spec = StrategySpec(
        name="dow_backtest",
        allocate=_allocate_selected if selected_universe else _allocate,
        eligible=eligible_products,
        rejection=_rejection,
        target_vol=target_vol,
        position_columns=POSITION_COLUMNS,
        scale_column=None,
        # 只有「持仓品种等分」的分母会随别人进出而动，才需要连锁调仓。
        resize_on_allocation_change=not selected_universe,
    )
    return run_portfolio_backtest(
        bundle=bundle,
        shadows=shadows,
        spec=spec,
        cost_bps=cost_bps,
        realized_vol_min_observations=realized_vol_min_observations,
        selection_observations=selection_observations,
    )
