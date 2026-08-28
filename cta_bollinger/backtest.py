"""The selected Bollinger portfolio: fixed-universe capital, 10% target vol.

Only the policy lives here. Capital is split across the *whole* selected
universe, so a selected product that is flat leaves its sleeve in cash rather
than handing it to the products that do have a signal (fidelity rule B3), and
the monthly multiplier targets 10%. The event loop, the parallel
pre-volatility ledger, rolls, and the month boundary are shared with the Dow
replication in ``common.commodity.backtest``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from common.commodity.backtest import (
    COST_BPS,
    SELECTION_OBSERVATIONS,
    VOL_OBSERVATIONS,
    Allocation,
    BacktestResult,
    StrategySpec,
    run_portfolio_backtest,
)
from common.commodity.portfolio import fixed_universe_weights
from common.commodity.selection import ProductScore
from cta_bollinger.selection import eligible_products

__all__ = ["BacktestResult", "TARGET_ANNUAL_VOL", "run_backtest"]


#: 研报 §5.5：组合年化波动目标 10%（道氏那篇是 15%，所以不是共享常量）。
TARGET_ANNUAL_VOL = 0.10
_MIN_TRADES = 5

#: Bollinger 的 positions 表不带 active_products —— 它的分母是当月入选品种数，
#: 与"当前有几个品种在场"无关，写出来只会让人误读成道氏那套。
_POSITION_COLUMNS = (
    "trade_date",
    "month_start",
    "product",
    "contract",
    "selected",
    "universe_weight",
    "direction",
    "oi_scale",
    "atr_leverage",
    "realized_vol",
    "target_annual_vol",
    "vol_multiplier",
    "leverage",
    "target_weight",
    "actual_weight",
    "prevol_weight",
)


def _allocate(
    selected: tuple[str, ...], directions: Mapping[str, float]
) -> Allocation:
    unit = 1.0 / len(selected) if selected else 0.0
    return Allocation(
        signed=fixed_universe_weights(directions, universe=selected),
        sleeve={product: unit for product in selected},
        active_products=sum(1 for side in directions.values() if side),
    )


def _rejection(score: ProductScore) -> str | None:
    if score.trade_count < _MIN_TRADES:
        return "too_few_trades"
    if score.sharpe < 0.0 and score.calmar < 0.0:
        return "negative_risk_adjusted"
    return None


def run_backtest(
    *,
    bundle: Any,
    shadows: Mapping[str, Any],
    target_vol: float = TARGET_ANNUAL_VOL,
    cost_bps: float = COST_BPS,
    realized_vol_min_observations: int = VOL_OBSERVATIONS,
    selection_observations: int = SELECTION_OBSERVATIONS,
) -> BacktestResult:
    """Run the selected Bollinger portfolio over a bundle and its shadows."""
    spec = StrategySpec(
        name="bollinger_backtest",
        allocate=_allocate,
        eligible=eligible_products,
        rejection=_rejection,
        target_vol=target_vol,
        position_columns=_POSITION_COLUMNS,
        scale_column="state_oi_scale",
        resize_on_allocation_change=False,
    )
    return run_portfolio_backtest(
        bundle=bundle,
        shadows=shadows,
        spec=spec,
        cost_bps=cost_bps,
        realized_vol_min_observations=realized_vol_min_observations,
        selection_observations=selection_observations,
    )
