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

from cta_carry.legreturns import forward_only_chain

from citic_index.factor import basis_momentum, term_structure
from citic_index.index import accumulate
from citic_index.returns import chain_returns
from citic_index.legs import select_legs
from citic_index.universe import pool_membership
from citic_index.weights import assign_weights


@dataclass(frozen=True)
class ReplicaConfig:
    """Every switch the design doc's deviation table needs to be able to flip."""

    factor_kind: str = "basis_momentum"
    window: int = 500
    min_observations: int = 450
    smoothing: int = 1
    normalise_by_gap: bool = True
    t1_leg: str = "near_dominant"
    cadence: str = "daily"
    roll_blend: bool = True
    return_basis: str = "close_to_prev_settle"
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


def _leg_returns(prices, legs, prefix, *, basis, roll_blend) -> pd.DataFrame:
    picks = legs.loc[
        :, ["trade_date", "product", f"{prefix}_contract", f"{prefix}_delivery_yyyymm"]
    ].rename(
        columns={
            f"{prefix}_contract": "contract",
            f"{prefix}_delivery_yyyymm": "delivery_yyyymm",
        }
    )
    returns = chain_returns(
        prices, forward_only_chain(picks), basis=basis, roll_blend=roll_blend
    )
    return returns.loc[:, ["trade_date", "product", "product_return"]].rename(
        columns={"product_return": f"{prefix}_return"}
    )


@dataclass(frozen=True)
class ReplicaPanel:
    """Everything that does not depend on the factor's parameters.

    The pool, the legs, their chain returns and the index leg are fixed once the
    universe, the T1 rule and the return basis are chosen; R, the smoothing and
    the cadence only enter afterwards.  Splitting there turns a parameter sweep
    from ninety seconds a point into a few, which is the difference between
    scanning a grid and reporting one point of it.
    """

    prices: pd.DataFrame
    pool: pd.DataFrame
    legs: pd.DataFrame
    returns: pd.DataFrame


def build_panel(prices: pd.DataFrame, config: ReplicaConfig) -> ReplicaPanel:
    """The factor-independent half of the pipeline."""
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
            _leg_returns(
                prices, legs, prefix,
                basis=config.return_basis, roll_blend=config.roll_blend,
            ),
            on=["trade_date", "product"],
            how="left",
            validate="one_to_one",
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
    returns = chain_returns(
        prices, main_chain, basis=config.return_basis, roll_blend=config.roll_blend
    )
    return ReplicaPanel(prices=prices, pool=pool, legs=legs, returns=returns)


def build_from_panel(panel: ReplicaPanel, config: ReplicaConfig) -> ReplicaResult:
    """The factor-dependent half: rank, weigh and accumulate."""
    legs, pool = panel.legs, panel.pool
    if config.factor_kind == "basis_momentum":
        factor = basis_momentum(
            legs,
            window=config.window,
            min_observations=config.min_observations,
            normalise_by_gap=config.normalise_by_gap,
            smoothing=config.smoothing,
        )
    elif config.factor_kind == "term_structure":
        factor = term_structure(
            legs,
            lookback=config.window,
            min_observations=config.min_observations,
            normalise_by_gap=config.normalise_by_gap,
        )
        # The ranking layer speaks one vocabulary.  Both factors are ranked the
        # same way by the same 3.5 steps, so the control arm hands its column
        # over under the names that layer already uses, keeping its own beside
        # them so the output still says which factor produced the run.
        factor["basis_momentum"] = factor["term_structure"]
        factor["bm_ready"] = factor["ts_ready"]
    else:
        raise ValueError(f"unknown factor_kind {config.factor_kind!r}")
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

    index = accumulate(
        weights,
        panel.returns,
        base_date=config.base_date,
        base_value=config.base_value,
    )
    return ReplicaResult(
        pool=pool,
        legs=legs,
        factor=factor,
        weights=weights,
        returns=panel.returns,
        index=index,
    )


def build_replica(prices: pd.DataFrame, config: ReplicaConfig) -> ReplicaResult:
    """Both halves, for a single run."""
    return build_from_panel(build_panel(prices, config), config)
