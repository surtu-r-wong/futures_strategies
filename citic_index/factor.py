"""The basis-momentum factor, per CITIC 3.1 and 3.5 step 1.

    BM_it = [ prod(1 + r_T1) - prod(1 + r_T2) ] / (months between T1 and T2)

3.1 gives the bracket; 3.5 step 1 gives the division -- "计算合约池中的合约的基差
动量为 R_i / 远近主力合约的相隔月数".  The shipped leg computes the bracket and
skips the division, which is deviation 2, so the division is a switch.

Nothing here is ever padded.  A day the chain could not price is absent from the
window, not a zero return: filling it would hand a young product a lookback it
has not lived through and systematically flatter the longer windows (see the
2026-09-10 design doc, section 1.2).  Both legs are masked to the days on which
*both* priced, so the difference is always like for like.
"""

import numpy as np
import pandas as pd


FACTOR_COLUMNS = (
    "trade_date",
    "product",
    "month_gap",
    "t1_cumulative",
    "t2_cumulative",
    "bm_observations",
    "bm_ready",
    "basis_momentum",
)


def basis_momentum(
    legs: pd.DataFrame,
    *,
    window: int,
    min_observations: int,
    normalise_by_gap: bool = True,
) -> pd.DataFrame:
    """Per product-day factor value over a trailing `window` of chain returns."""
    if window < 1:
        raise ValueError("window must be at least one trading day")
    if min_observations < 1 or min_observations > window:
        raise ValueError("min_observations must be in [1, window]")
    if legs.empty:
        return pd.DataFrame(columns=list(FACTOR_COLUMNS))

    frame = legs.sort_values(["product", "trade_date"], kind="mergesort").copy()
    both = frame["t1_return"].notna() & frame["t2_return"].notna()

    for leg in ("t1", "t2"):
        masked = np.log1p(frame[f"{leg}_return"].where(both))
        frame[f"{leg}_cumulative"] = np.expm1(
            masked.groupby(frame["product"], sort=False)
            .rolling(window, min_periods=1)
            .sum()
            .reset_index(level=0, drop=True)
        )

    frame["bm_observations"] = (
        both.groupby(frame["product"], sort=False)
        .rolling(window, min_periods=1)
        .sum()
        .reset_index(level=0, drop=True)
        .astype("Int64")
    )
    # One gate, not two.  The rolling sums are taken with min_periods=1 so they
    # carry whatever the window actually holds, and the observation count alone
    # decides whether that is enough.  An earlier version also set min_periods
    # to the floor, which enforced the same rule a second time -- redundant, and
    # untestable: loosening either one alone left the other holding the gate, so
    # neither mutation could be caught.
    frame["bm_ready"] = (
        frame["bm_observations"].ge(min_observations).fillna(False).astype(bool)
    )

    raw = frame["t1_cumulative"] - frame["t2_cumulative"]
    if normalise_by_gap:
        raw = raw / frame["month_gap"]
    frame["basis_momentum"] = raw.where(frame["bm_ready"])

    frame = frame.sort_values(["trade_date", "product"], kind="mergesort")
    return frame.loc[:, list(FACTOR_COLUMNS)].reset_index(drop=True)
