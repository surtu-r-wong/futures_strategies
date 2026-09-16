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

from citic_index.factor import (
    basis_momentum,
    term_structure,
    time_series_momentum,
    warehouse_receipt,
)
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
    # 023's baseline window, per 3.1's "前 300 个交易日到前 200 个交易日".  The
    # phrase is a day ambiguous at each end; this reads the 100 trading days
    # ending at t-200.  Only the warehouse_receipt factor reads these.
    baseline_lag: int = 200
    baseline_window: int = 100
    # Where 023's p-day mean is taken: "level" is its own wording (average the
    # receipt counts, divide once), "ratio" is 025's placement (average the
    # daily factor).  See citic_index.factor.warehouse_receipt.
    smoothing_target: str = "level"
    # 026 only: the volatility window 3.5 step 3 never defines.
    vol_window: int = 60
    min_observations: int = 450
    smoothing: int = 1
    normalise_by_gap: bool = True
    t1_leg: str = "near_dominant"
    cadence: str = "daily"
    roll_blend: bool = True
    # settle_to_settle is the replication basis (user ruling 2026-09-16): it is
    # what the official daily series follows on all four indices.  Production
    # books set close_to_close explicitly, the only convention that can be traded.
    return_basis: str = "settle_to_settle"
    # Trading days between the signal day and the day the book is struck.
    # 1 = signal on T, traded at T+1's price, first return T+1 -> T+2: the only
    # convention that can be executed, since the signal needs T's close (or
    # settlement) to exist.  0 strikes the book at the very price the signal
    # was computed on and is kept only as a diagnostic (user ruling 2026-09-16,
    # after that convention inflated the time-series momentum replica).
    execution_lag: int = 1
    liquidity_window: int = 20
    liquidity_threshold: float = 2e9
    min_listing_calendar_days: int = 90
    restrict_to_named: bool = True
    min_products: int = 2
    exclude_limit_locked: bool = True
    # Sizing, mirroring the production carry runner so two books facing the
    # same question answer it the same way.
    target_vol: float = 0.15
    vol_window: int = 252
    max_gross_leverage: float = 4.0
    cost_bps: float = 4.0
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
    # Raw receipt levels do not depend on p, so they belong to the panel: a
    # sweep over p loads them once.
    receipts: pd.DataFrame | None = None


def build_panel(
    prices: pd.DataFrame,
    config: ReplicaConfig,
    *,
    receipts: pd.DataFrame | None = None,
) -> ReplicaPanel:
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
    return ReplicaPanel(
        prices=prices, pool=pool, legs=legs, returns=returns, receipts=receipts
    )


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
    elif config.factor_kind == "time_series_momentum":
        factor = time_series_momentum(
            panel.returns,
            lookback=config.window,
            vol_window=config.vol_window,
        )
        # The ranking layer's vocabulary again, but 026 does not rank: the
        # column only orders ties, and `equal_vol` reads sigma and direction.
        factor["basis_momentum"] = factor["ts_momentum"]
        factor["bm_ready"] = factor["ts_ready"]
    elif config.factor_kind == "warehouse_receipt":
        if panel.receipts is None:
            raise ValueError(
                "citic_pipeline: factor_kind 'warehouse_receipt' needs a panel "
                "built with receipts=..."
            )
        factor = warehouse_receipt(
            panel.receipts,
            lookback=config.window,
            baseline_lag=config.baseline_lag,
            baseline_window=config.baseline_window,
            smoothing_target=config.smoothing_target,
        )
        # Same handover as the control arm -- the ranking layer speaks one
        # vocabulary -- but with the sign flipped, and that is not cosmetic.
        #
        # 023's 3.5 step 2 sorts 从大到小 while the ranking layer sorts
        # ascending, so the factor is negated to reproduce the paper's
        # direction: the largest receipt growth takes Rank 1 and the most
        # negative weight, which is what 第 1 页 asks for -- "选择仓单增加的商品
        # 做空（做空当前库存充足的品种）".
        #
        # The trap is that step 3's weight formula is word-for-word 027's, so
        # reusing the section wholesale looks safe and silently inverts the
        # strategy.  Measured: without the negation the full-history replica
        # correlates -0.50 with the published series rather than +0.50.
        factor["basis_momentum"] = -factor["warehouse_receipt"]
        factor["bm_ready"] = factor["wr_ready"]
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

    ranked = factor.assign(bm_ready=factor["rankable"])
    if config.exclude_limit_locked:
        ranked = ranked.merge(
            legs.loc[:, ["trade_date", "product", "main_limit_locked"]].rename(
                columns={"main_limit_locked": "limit_locked"}
            ),
            on=["trade_date", "product"],
            how="left",
        )
    weights = assign_weights(
        ranked,
        cadence=config.cadence,
        min_products=config.min_products,
        scheme=(
            "equal_vol"
            if config.factor_kind == "time_series_momentum"
            else "rank_linear"
        ),
    )

    index = accumulate(
        weights,
        panel.returns,
        base_date=config.base_date,
        base_value=config.base_value,
        execution_lag=config.execution_lag,
    )
    return ReplicaResult(
        pool=pool,
        legs=legs,
        factor=factor,
        weights=weights,
        returns=panel.returns,
        index=index,
    )


def build_replica(
    prices: pd.DataFrame,
    config: ReplicaConfig,
    *,
    receipts: pd.DataFrame | None = None,
) -> ReplicaResult:
    """Both halves, for a single run."""
    return build_from_panel(build_panel(prices, config, receipts=receipts), config)
