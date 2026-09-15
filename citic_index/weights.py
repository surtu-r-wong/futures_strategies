"""Cross-sectional weights, per CITIC 3.5 steps 2 and 3.

    sort the factor ascending -> Rank_i in 1..N
    w_i = (Rank_i - (1+N)/2) / (N(1+N)/2)

Zero-sum by construction, and the largest factor value takes the largest
positive weight -- higher basis momentum predicts a higher return on the near
contract, so that is the side the economics want.

3.5 walks these steps for every T+1, so the replica reranks daily.  The shipped
basis-momentum leg strikes once a month and holds, a cadence taken from the
Boons & Prado paper rather than from this methodology; that is deviation 3 and
`cadence="monthly"` reproduces it, re-centring the survivors when a product
drops out mid-month exactly as the production leg does.

A `limit_locked` column, when present, takes those products out of the struck
cross-section -- 3.2's "将其排除在策略之外" -- which also shrinks N and so moves
everyone else's weight.  It bites only on a strike day.
"""

import pandas as pd

# Both imported rather than restated: the weight formula because the production
# path already spells it out and two spellings could drift apart, and the
# rebalance calendar because the monthly arm is only useful for attribution if
# it strikes on the same days the production leg strikes.
from cta_carry.signals import _rebalance_dates, rank_linear_weights


WEIGHT_COLUMNS = (
    "trade_date",
    "product",
    "basis_momentum",
    "n_products",
    "weight",
)

_CADENCES = ("daily", "monthly")
_SCHEMES = ("rank_linear", "equal_vol")


def assign_weights(
    factor: pd.DataFrame,
    *,
    cadence: str = "daily",
    min_products: int = 2,
    scheme: str = "rank_linear",
) -> pd.DataFrame:
    """One row per ranked product-day with its index weight.

    `scheme` picks 3.3's sizing: `rank_linear` is 023/025/027's zero-sum rank
    weights, `equal_vol` is 026's `w_i proportional to 1/sigma_i^2` signed by
    each product's own direction, which carries net exposure.
    """
    if cadence not in _CADENCES:
        raise ValueError(f"cadence must be one of {_CADENCES}, got {cadence!r}")
    if scheme not in _SCHEMES:
        raise ValueError(f"scheme must be one of {_SCHEMES}, got {scheme!r}")
    if factor.empty:
        return pd.DataFrame(columns=list(WEIGHT_COLUMNS))

    ready = factor.loc[factor["bm_ready"].astype(bool)].copy()
    if ready.empty:
        return pd.DataFrame(columns=list(WEIGHT_COLUMNS))

    rebalance_days = (
        set(ready["trade_date"].unique())
        if cadence == "daily"
        else _rebalance_dates(ready["trade_date"], cadence)
    )

    struck: dict = {}
    rows = []
    for trade_date, day in ready.groupby("trade_date", sort=True):
        cross_section = day.sort_values(["basis_momentum", "product"], kind="mergesort")
        if len(cross_section) < min_products:
            continue
        if trade_date in rebalance_days:
            # 3.2's special adjustment, judged where the document puts it: on
            # the day the weights are struck.  A product locked up after the
            # strike is one you cannot trade, not one you have stopped holding,
            # so it keeps whatever it was given.
            eligible = cross_section
            if "limit_locked" in cross_section.columns:
                eligible = cross_section.loc[
                    ~cross_section["limit_locked"].fillna(False).astype(bool)
                ]
            if len(eligible) < min_products:
                continue
            sized = (
                rank_linear_weights(eligible, "basis_momentum")
                if scheme == "rank_linear"
                else equal_vol_weights(eligible)
            )
            struck = dict(zip(eligible["product"], sized.to_numpy()))
        held = cross_section["product"].map(struck)
        surviving = held.dropna()
        if surviving.empty:
            continue
        if len(surviving) != len(struck):
            held = held.copy()
            if scheme == "rank_linear":
                # Someone left since the strike, so what is left no longer sums
                # to zero.  Recentre rather than carrying a net position.
                held.loc[surviving.index] = surviving - surviving.mean()
            else:
                # 026 is not zero-sum, so recentring would be wrong here: what
                # 3.3 fixes is that the sizes total one.  Renormalise instead.
                total = surviving.abs().sum()
                if total > 0:
                    held.loc[surviving.index] = surviving / total
        cross_section = cross_section.assign(
            weight=held.to_numpy(),
            # N is the size of the cross-section the weights were struck over --
            # it is in the formula -- not the number of rows standing today.
            n_products=len(struck),
        )
        rows.append(cross_section.loc[cross_section["weight"].notna()])

    if not rows:
        return pd.DataFrame(columns=list(WEIGHT_COLUMNS))
    out = pd.concat(rows, ignore_index=True)
    out = out.sort_values(["trade_date", "product"], kind="mergesort")
    return out.loc[:, list(WEIGHT_COLUMNS)].reset_index(drop=True)


def equal_vol_weights(cross_section: pd.DataFrame) -> pd.Series:
    """CITIC 026's 3.3: equal-volatility sizing, signed by 3.5 step 2.

        w_1 sigma_1^2 = ... = w_N sigma_N^2,  sum(w) = 1

    which solves to `w_i proportional to 1/sigma_i^2`, normalised.  The sizes
    are set by volatility alone; `direction` only orients them, so a product
    sitting flat keeps its share of the budget at zero weight rather than
    handing it to the others.

    **The result is not zero-sum.** 3.3 fixes the sizes to sum to one and 3.5
    step 2 signs each independently, so 026 carries net exposure -- unlike
    023/025/027, whose rank weights cancel by construction.
    """
    if cross_section.empty:
        return pd.Series(dtype="float64")
    sigma = cross_section["sigma"].astype("float64")
    if not (sigma > 0).all():
        raise ValueError("equal_vol_weights: sigma must be positive for every product")
    inverse_variance = 1.0 / sigma.pow(2)
    sizes = inverse_variance / inverse_variance.sum()
    return sizes * cross_section["direction"].astype("float64")
