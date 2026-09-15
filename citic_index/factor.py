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


TERM_STRUCTURE_COLUMNS = (
    "trade_date",
    "product",
    "month_gap",
    "roll_yield",
    "ts_observations",
    "ts_ready",
    "term_structure",
)

WAREHOUSE_RECEIPT_COLUMNS = (
    "trade_date",
    "product",
    "wr_recent",
    "wr_baseline",
    "wr_observations",
    "wr_ready",
    "warehouse_receipt",
)

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
    smoothing: int = 1,
) -> pd.DataFrame:
    """Per product-day factor value over a trailing `window` of chain returns.

    `smoothing` is the lookback 3.1 gives the strategy beside R: the factor is
    the arithmetic mean of BM over that many days.  027 writes "回望周期即将回望
    周期内的基差动量值作为当前的...因子值" and leaves out the four characters for
    "arithmetic mean" that 025's otherwise identical sentence carries, and 025's
    step 2 says it again -- "求出 p 日(参数)...的平均值".  The two methodologies
    are word for word the same through 3.2 to 3.5, so the omission is an
    omission.  `smoothing=1` is the unsmoothed reading.
    """
    if window < 1:
        raise ValueError("window must be at least one trading day")
    if min_observations < 1 or min_observations > window:
        raise ValueError("min_observations must be in [1, window]")
    if smoothing < 1:
        raise ValueError("smoothing must be at least one trading day")
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
    raw = raw.where(frame["bm_ready"])
    if smoothing > 1:
        # min_periods is the full window: averaging fewer days would hand a
        # young product a shorter lookback than everyone it is ranked against,
        # silently, which is the same defect the history gate exists to stop.
        raw = (
            raw.groupby(frame["product"], sort=False)
            .rolling(smoothing, min_periods=smoothing)
            .mean()
            .reset_index(level=0, drop=True)
        )
        frame["bm_ready"] = frame["bm_ready"] & raw.notna()
    frame["basis_momentum"] = raw

    frame = frame.sort_values(["trade_date", "product"], kind="mergesort")
    return frame.loc[:, list(FACTOR_COLUMNS)].reset_index(drop=True)


def term_structure(
    legs: pd.DataFrame,
    *,
    lookback: int,
    min_observations: int,
    normalise_by_gap: bool = True,
) -> pd.DataFrame:
    """CITIC 025's factor, the control arm for this engine.

    3.5 step 1 gives the roll yield as
    `(P_near - P_far) / P_near / months apart`, and step 2 ranks the arithmetic
    mean of it over a p-day lookback -- "求出 p 日(参数)展期收益率 R_i 的平均值
    meanR_i".  3.1 writes the same quantity with 12/(months apart) as an
    exponent and annualised; the annualisation is one constant across the whole
    cross-section, so it cannot move a rank and 3.5 is followed instead.

    This exists to check the engine, not to ship a second strategy.  025 is
    known replicable to 9.37%/1.90 against an official 9.31%/1.89, so a run
    that cannot reproduce it says the fault is here rather than in 027.
    """
    if lookback < 1:
        raise ValueError("lookback must be at least one trading day")
    if min_observations < 1 or min_observations > lookback:
        raise ValueError("min_observations must be in [1, lookback]")
    if legs.empty:
        return pd.DataFrame(columns=list(TERM_STRUCTURE_COLUMNS))

    frame = legs.sort_values(["product", "trade_date"], kind="mergesort").copy()
    raw = (frame["t1_close"] - frame["t2_close"]) / frame["t1_close"]
    if normalise_by_gap:
        raw = raw / frame["month_gap"]
    frame["roll_yield"] = raw

    grouped = frame["roll_yield"].groupby(frame["product"], sort=False)
    frame["ts_observations"] = (
        frame["roll_yield"].notna()
        .groupby(frame["product"], sort=False)
        .rolling(lookback, min_periods=1)
        .sum()
        .reset_index(level=0, drop=True)
        .astype("Int64")
    )
    mean = (
        grouped.rolling(lookback, min_periods=1).mean().reset_index(level=0, drop=True)
    )
    frame["ts_ready"] = (
        frame["ts_observations"].ge(min_observations).fillna(False).astype(bool)
    )
    frame["term_structure"] = mean.where(frame["ts_ready"])

    frame = frame.sort_values(["trade_date", "product"], kind="mergesort")
    return frame.loc[:, list(TERM_STRUCTURE_COLUMNS)].reset_index(drop=True)


def warehouse_receipt(
    receipts: pd.DataFrame,
    *,
    lookback: int,
    baseline_lag: int = 200,
    baseline_window: int = 100,
    smoothing_target: str = "level",
) -> pd.DataFrame:
    """CITIC 023's factor, per 3.1 and 3.5 step 1.

        TS_it = mean(C, last p days) / mean(C, the t-300..t-200 window) - 1

    `C` is the standard warehouse receipt quantity, a daily stock.  3.1 names
    the denominator "前 300 个交易日到前 200 个交易日的平均仓单数量", which is a
    day ambiguous at each end; this reads it as the 100 trading days ending at
    t-200, i.e. [t-299, t-200].  A neighbouring reading moves a 100-day mean by
    one day and cannot move a rank.

    **Absence is filled upstream, not here.**  That is the opposite of the rule
    this module opens with, and the difference is in the data rather than in the
    taste: a day the price chain could not price has no return, but a day a
    receipt series omits usually *is* a zero -- DCE writes one zero and then
    stops emitting rows, so 25.4% of its product-days are zero while only 2.1%
    say so.  `citic_index.receipts` resolves that before the factor sees it.  A
    NaN reaching here is therefore a genuine gap and blocks the day.

    A zero *value*, by contrast, is signal: receipts really do go to zero on a
    mass cancellation, and 3.5 step 2 ranks that to the long end.  Only a zero
    *baseline* is refused, because the ratio has nothing to divide by -- the
    paper gives no guard for it (§3.1 has none and §3.2's special adjustments
    cover only limit-locked and delisted products), so the house rule applies:
    no evidence, no coverage.

    ``smoothing_target`` is where the p-day mean is taken, and the two readings
    are not equivalent once the baseline moves:

    - ``"level"`` (default, 023's own words) averages the receipt counts and
      divides once: 3.1's "多少个回望周期 p 的平均仓单数量", repeated by 3.5
      step 1 as "仓单平均值/仓单平均值".
    - ``"ratio"`` averages the daily factor instead, which is what 025's 3.1
      prescribes -- "回望周期即将回望周期内的期限结构值算术平均值作为…因子值" --
      and what ``term_structure`` above implements.

    The second is worth being able to test because 023's document is demonstrably
    a rewrite of 025's: it carries 025's 升贴水 through a sentence whose every
    other term was changed to receipts.  A rewrite of the *code* that kept 025's
    placement of the mean would look exactly like this switch.  The default
    follows 023's own words; anything else has to be argued from evidence.
    """
    if lookback < 1:
        raise ValueError("lookback must be at least one trading day")
    if baseline_lag < 1:
        raise ValueError("baseline_lag must be at least one trading day")
    if baseline_window < 1:
        raise ValueError("baseline_window must be at least one trading day")
    if smoothing_target not in ("level", "ratio"):
        raise ValueError(
            f"smoothing_target must be 'level' or 'ratio'; got {smoothing_target!r}"
        )
    if receipts.empty:
        return pd.DataFrame(columns=list(WAREHOUSE_RECEIPT_COLUMNS))

    frame = receipts.sort_values(
        ["product", "trade_date"], kind="mergesort"
    ).copy()
    values = frame["receipts"].astype("float64")
    by_product = values.groupby(frame["product"], sort=False)

    def _per_product(series, fn):
        return fn(series).reset_index(level=0, drop=True)

    # min_periods equal to the window means a NaN anywhere in it yields NaN,
    # so a gap cannot be averaged away by the surviving days.
    shifted = values.groupby(frame["product"], sort=False).shift(baseline_lag)
    frame["wr_baseline"] = _per_product(
        shifted.groupby(frame["product"], sort=False),
        lambda g: g.rolling(baseline_window, min_periods=baseline_window).mean(),
    )
    frame["wr_observations"] = (
        _per_product(
            shifted.notna().groupby(frame["product"], sort=False),
            lambda g: g.rolling(baseline_window, min_periods=1).sum(),
        )
        .astype("Int64")
    )
    if smoothing_target == "level":
        frame["wr_recent"] = _per_product(
            by_product, lambda g: g.rolling(lookback, min_periods=lookback).mean()
        )
        frame["wr_ready"] = (
            frame["wr_recent"].notna()
            & frame["wr_baseline"].notna()
            & frame["wr_baseline"].gt(0)
        ).fillna(False).astype(bool)
        raw = frame["wr_recent"] / frame["wr_baseline"] - 1.0
    else:
        # Each day divided by the baseline standing under it, then averaged.
        daily = (values / frame["wr_baseline"] - 1.0).where(frame["wr_baseline"].gt(0))
        frame["wr_recent"] = _per_product(
            values.groupby(frame["product"], sort=False),
            lambda g: g.rolling(lookback, min_periods=lookback).mean(),
        )
        raw = _per_product(
            daily.groupby(frame["product"], sort=False),
            lambda g: g.rolling(lookback, min_periods=lookback).mean(),
        )
        frame["wr_ready"] = raw.notna().fillna(False).astype(bool)
    frame["warehouse_receipt"] = raw.where(frame["wr_ready"])

    frame = frame.sort_values(["trade_date", "product"], kind="mergesort")
    return frame.loc[:, list(WAREHOUSE_RECEIPT_COLUMNS)].reset_index(drop=True)

