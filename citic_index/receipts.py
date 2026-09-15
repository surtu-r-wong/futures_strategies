"""What an absent warehouse receipt day means, resolved before the factor sees it.

`citic_index.factor` opens by saying nothing is ever padded, and for the price
chain that is right: a day the chain could not price has no return, and filling
it hands a young product a lookback it never lived through.  Receipts are the
other case.  Exchanges disagree about how to write a zero, and the disagreement
is large enough to break a cross-sectional rank:

    exchange  coverage  explicit zeros  zero-or-absent
    CZCE       100.00%          22.21%          22.21%
    DCE         76.15%           2.08%          25.43%
    SHFE        99.99%           3.15%           3.15%

CZCE writes its zeros; DCE writes one zero and then stops emitting rows until
receipts return.  Averaging "the days that have observations" therefore lifts
every DCE product's mean against every CZCE product's, and the paper ranks the
two against each other.

The rule reads the observation preceding a gap, not the exchange it came from.
Two findings rule out the per-exchange shortcut: RU on SHFE has three absent
days bracketed by 26,500 -- filling those would fabricate a collapse -- and five
DCE products carry genuine gaps of their own (C 94, L 90, Y 80, M 76, P 12).
Reading the run keeps the rule working when a product or an exchange is added.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Sequence

import pandas as pd

COLUMNS = ["trade_date", "product", "receipts"]


def fill_absent_zeros(
    observations: pd.DataFrame,
    calendar: Sequence[date] | Iterable[date],
    *,
    through: date | None = None,
    with_report: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, dict]:
    """Resolve absences into zeros or genuine gaps, per product.

    Each product is laid on the trading calendar from its first observation to
    its last -- or to `through`, when the caller wants the tail resolved as well.
    A day with no observation takes the value zero if the most recent
    observation before it was zero, and stays missing otherwise.

    `through` exists for one shape seen in the real data: JD's last observation
    is a zero on 2026-08-05 while 36 of 37 products run to 09-14.  Its 18.4%
    zero rate and single-digit magnitudes make that a real zero rather than a
    stalled feed, and without resolving the tail it drops out of the current
    cross-section.  Days before a product's first observation are never filled:
    it had not listed.

    Passing `through` does not extrapolate a delisted product indefinitely in
    practice -- 3.2's liquidity gate drops it long before the zeros matter.
    """
    frame = observations.loc[:, COLUMNS] if len(observations.columns) else observations
    if frame.empty:
        empty = pd.DataFrame(columns=COLUMNS)
        return (empty, {"filled_zero": 0, "left_missing": 0}) if with_report else empty

    dupes = frame.duplicated(["product", "trade_date"])
    if dupes.any():
        first = frame.loc[dupes, ["product", "trade_date"]].iloc[0]
        raise ValueError(
            "citic_receipts: duplicate product-day "
            f"{first['product']!r} {first['trade_date']}"
        )

    days = pd.DatetimeIndex(sorted({pd.Timestamp(d) for d in calendar}))
    cutoff = pd.Timestamp(through) if through is not None else None

    resolved, filled_zero, left_missing = [], 0, 0
    for product, group in frame.groupby("product", sort=True):
        series = (
            group.assign(trade_date=pd.to_datetime(group["trade_date"]))
            .set_index("trade_date")["receipts"]
            .astype("float64")
            .sort_index()
        )
        last = series.index.max() if cutoff is None else max(series.index.max(), cutoff)
        # No clamp to the calendar's end is needed: `span` is filtered out of
        # `days`, so a cutoff past it cannot introduce a day that does not
        # exist.  (An earlier `min(last, days.max())` here was an equivalent
        # mutation -- it read like a bound and did nothing.)
        span = days[(days >= series.index.min()) & (days <= last)]
        laid = series.reindex(span)

        absent = laid.isna()
        # The run before the gap decides.  ffill carries the last observation
        # forward; where that is zero, the silence means zero.
        preceding = laid.ffill()
        fill = absent & preceding.eq(0.0)
        laid = laid.where(~fill, 0.0)

        filled_zero += int(fill.sum())
        left_missing += int((absent & ~fill).sum())
        resolved.append(
            pd.DataFrame(
                {
                    "trade_date": [d.date() for d in span],
                    "product": product,
                    "receipts": laid.to_numpy(),
                }
            )
        )

    out = (
        pd.concat(resolved, ignore_index=True)
        .sort_values(["trade_date", "product"], kind="mergesort")
        .reset_index(drop=True)
        .loc[:, COLUMNS]
    )
    if with_report:
        return out, {"filled_zero": filled_zero, "left_missing": left_missing}
    return out
