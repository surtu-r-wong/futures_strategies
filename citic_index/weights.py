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


def assign_weights(
    factor: pd.DataFrame,
    *,
    cadence: str = "daily",
    min_products: int = 2,
) -> pd.DataFrame:
    """One row per ranked product-day with its index weight."""
    if cadence not in _CADENCES:
        raise ValueError(f"cadence must be one of {_CADENCES}, got {cadence!r}")
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
        cross_section = day.sort_values(
            ["basis_momentum", "product"], kind="mergesort"
        )
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
            struck = dict(
                zip(
                    eligible["product"],
                    rank_linear_weights(eligible, "basis_momentum").to_numpy(),
                )
            )
        held = cross_section["product"].map(struck)
        surviving = held.dropna()
        if surviving.empty:
            continue
        if len(surviving) != len(struck):
            # Someone left since the strike, so what is left no longer sums to
            # zero.  Recentre the survivors rather than carrying a net position.
            held = held.copy()
            held.loc[surviving.index] = surviving - surviving.mean()
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
