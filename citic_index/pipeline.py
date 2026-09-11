"""The five layers wired together into one index series.

Ordering matters in one place that is easy to get wrong.  Legs and chain
returns are built over *every* product-day, not just the pooled ones: a product
that drops below the liquidity threshold for a month and comes back has not
stopped having a term structure, and interrupting its chain there would punch a
hole in a five-hundred-day momentum window that takes two years to heal.  The
pool gate is applied where 3.2 puts it -- on who may be ranked today.

3.4 says the index itself is computed from the dominant contract's data, so the
index leg is the main chain, while the factor differences the T1 and T2 chains.
"""

from dataclasses import dataclass
from datetime import date

import pandas as pd

from cta_carry.legreturns import build_leg_returns, forward_only_chain

from citic_index.factor import basis_momentum
from citic_index.index import accumulate, blended_returns
from citic_index.legs import select_legs
from citic_index.universe import pool_membership
from citic_index.weights import assign_weights


@dataclass(frozen=True)
class ReplicaConfig:
    """Every switch the design doc's deviation table needs to be able to flip."""

    window: int = 500
    min_observations: int = 450
    normalise_by_gap: bool = True
    t1_leg: str = "near_dominant"
    cadence: str = "daily"
    roll_blend: bool = True
    liquidity_window: int = 20
    liquidity_threshold: float = 2e9
    min_listing_calendar_days: int = 90
    restrict_to_named: bool = True
    min_products: int = 2
    base_date: date = date(2010, 1, 4)
    base_value: float = 1000.0

    def as_dict(self) -> dict:
        return {
            field: getattr(self, field) for field in self.__dataclass_fields__
        }


@dataclass(frozen=True)
class ReplicaResult:
    pool: pd.DataFrame
    legs: pd.DataFrame
    factor: pd.DataFrame
    weights: pd.DataFrame
    returns: pd.DataFrame
    index: pd.DataFrame


def _chain_returns(prices: pd.DataFrame, legs: pd.DataFrame, prefix: str) -> pd.DataFrame:
    picks = legs.loc[
        :, ["trade_date", "product", f"{prefix}_contract", f"{prefix}_delivery_yyyymm"]
    ].rename(
        columns={
            f"{prefix}_contract": "contract",
            f"{prefix}_delivery_yyyymm": "delivery_yyyymm",
        }
    )
    returns = build_leg_returns(prices, forward_only_chain(picks))
    return returns.loc[:, ["trade_date", "product", "leg_return"]].rename(
        columns={"leg_return": f"{prefix}_return"}
    )


def build_replica(prices: pd.DataFrame, config: ReplicaConfig) -> ReplicaResult:
    """Run the whole pipeline over normalised contract bars."""
    pool = pool_membership(
        prices,
        liquidity_window=config.liquidity_window,
        threshold=config.liquidity_threshold,
        min_listing_calendar_days=config.min_listing_calendar_days,
        restrict_to_named=config.restrict_to_named,
    )

    legs = select_legs(prices, t1_leg=config.t1_leg)
    for prefix in ("t1", "t2"):
        legs = legs.merge(
            _chain_returns(prices, legs, prefix),
            on=["trade_date", "product"],
            how="left",
            validate="one_to_one",
        )

    factor = basis_momentum(
        legs,
        window=config.window,
        min_observations=config.min_observations,
        normalise_by_gap=config.normalise_by_gap,
    )
    factor = factor.merge(
        pool.loc[:, ["trade_date", "product", "in_pool"]],
        on=["trade_date", "product"],
        how="left",
    )
    factor["in_pool"] = factor["in_pool"].fillna(False).astype(bool)
    # 3.2 gates who may be ranked; the factor itself is computed regardless.
    factor["rankable"] = factor["bm_ready"].astype(bool) & factor["in_pool"]

    weights = assign_weights(
        factor.assign(bm_ready=factor["rankable"]),
        cadence=config.cadence,
        min_products=config.min_products,
    )

    main_chain = forward_only_chain(
        legs.loc[
            :, ["trade_date", "product", "main_contract", "main_delivery_yyyymm"]
        ].rename(
            columns={
                "main_contract": "contract",
                "main_delivery_yyyymm": "delivery_yyyymm",
            }
        )
    )
    returns = blended_returns(prices, main_chain, roll_blend=config.roll_blend)

    index = accumulate(
        weights,
        returns,
        base_date=config.base_date,
        base_value=config.base_value,
    )
    return ReplicaResult(
        pool=pool, legs=legs, factor=factor, weights=weights, returns=returns, index=index
    )
