"""Preset CTA strategies from the referenced factor-combo product."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from cta_gtja.backtest import CTABacktester, CTABacktestResult, forward_open_returns, portfolio_returns
from cta_gtja.data import CTADataSet
from cta_gtja.factors import CTAFactor, default_cta_factors
from cta_gtja.portfolio import (
    blend_allocations,
    combine_factor_weights,
    equal_factor_allocations,
    factor_momentum_allocations,
    factor_weights,
)


@dataclass(frozen=True)
class CTAStrategySpec:
    name: str
    target_vol: float
    max_leverage: float
    cost_bps: float = 1.0


MEDIUM_EQUAL_WEIGHT = CTAStrategySpec(
    name="cta_medium_equal_weight",
    target_vol=0.08,
    max_leverage=2.50,
)

HIGH_COMPOSITE = CTAStrategySpec(
    name="cta_high_composite",
    target_vol=0.12,
    max_leverage=3.50,
)


def run_medium_equal_weight(
    data: CTADataSet,
    *,
    symbols: list[str] | None = None,
    factors: list[CTAFactor] | None = None,
    cost_bps: float = 1.0,
) -> CTABacktestResult:
    """CTA因子组合（中波等权）.

    Replication of the deck's 8% target-volatility product: six factor sleeves
    are equal-weighted, then the final portfolio is volatility targeted.
    """
    factors = factors or default_cta_factors()
    weights_by_factor, factor_returns = build_factor_sleeves(data, factors=factors, symbols=symbols)
    allocations = equal_factor_allocations(next(iter(weights_by_factor.values())).index, list(weights_by_factor))
    weights = combine_factor_weights(weights_by_factor, allocations)
    return CTABacktester(
        data,
        cost_bps=cost_bps,
        target_vol=MEDIUM_EQUAL_WEIGHT.target_vol,
        max_leverage=MEDIUM_EQUAL_WEIGHT.max_leverage,
    ).run(weights, factor_allocations=allocations, factor_returns=factor_returns)


def run_high_composite(
    data: CTADataSet,
    *,
    symbols: list[str] | None = None,
    factors: list[CTAFactor] | None = None,
    cost_bps: float = 1.0,
    smart_beta_weight: float = 0.60,
    rotation_lookback_days: int = 60,
    rotation_top_n: int = 2,
    max_single_factor_weight: float = 0.50,
) -> CTABacktestResult:
    """CTA因子组合2号（高波复合）.

    Replication of the deck's "60%等权组合 + 40%轮动策略" product.  The
    rotation sleeve ranks factor sleeves by recent standalone performance and
    caps a single factor at 50%.
    """
    factors = factors or default_cta_factors()
    weights_by_factor, factor_returns = build_factor_sleeves(data, factors=factors, symbols=symbols)
    index = next(iter(weights_by_factor.values())).index
    factor_names = list(weights_by_factor)
    smart_beta = equal_factor_allocations(index, factor_names)
    rotation = factor_momentum_allocations(
        factor_returns.reindex(index),
        lookback_days=rotation_lookback_days,
        top_n=rotation_top_n,
        max_single_weight=max_single_factor_weight,
    )
    allocations = blend_allocations(
        smart_beta,
        rotation,
        smart_beta_weight=smart_beta_weight,
        max_single_weight=max_single_factor_weight,
    ).reindex(index).fillna(0.0)
    weights = combine_factor_weights(weights_by_factor, allocations)
    return CTABacktester(
        data,
        cost_bps=cost_bps,
        target_vol=HIGH_COMPOSITE.target_vol,
        max_leverage=HIGH_COMPOSITE.max_leverage,
    ).run(weights, factor_allocations=allocations, factor_returns=factor_returns)


def build_factor_sleeves(
    data: CTADataSet,
    *,
    factors: list[CTAFactor],
    symbols: list[str] | None = None,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    symbols = symbols or data.symbols
    if not symbols:
        raise ValueError("CTA strategy needs at least one symbol")
    weights_by_factor: dict[str, pd.DataFrame] = {}
    for factor in factors:
        scores = factor.compute(data, symbols)
        weights_by_factor[factor.name] = factor_weights(factor, scores).reindex(columns=symbols).fillna(0.0)

    asset_returns = forward_open_returns(data, symbols)
    factor_returns = pd.DataFrame({
        name: portfolio_returns(weights, asset_returns)
        for name, weights in weights_by_factor.items()
    })
    return weights_by_factor, factor_returns

