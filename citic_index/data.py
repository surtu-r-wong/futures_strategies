"""Fetching the replica's input, and proving the dump is what the database has.

This machine reaches the database over a DERP relay that cannot hold a
connection for minutes at a time but serves short queries in half a second, so
the pull is sliced by date, each slice retried on its own short connection.
A dump assembled that way is only usable if it is checked, hence `reconcile`:
month-by-month row counts against the same predicate run upstream.  An
unreconciled bundle must never produce a published number.
"""

from datetime import date, timedelta

import pandas as pd

# The replica reads its bars through the same normaliser the production
# public-pg path uses, so a bundle and a live query cannot diverge in how a
# contract code is parsed -- Zhengzhou's single-digit delivery year in
# particular, which only resolves against the trade date.
from cta_carry.data import normalize_contract_daily  # noqa: F401  (re-exported)


def date_chunks(start: date, end: date, *, chunk_days: int) -> list[tuple[date, date]]:
    """Tile [start, end] into consecutive closed intervals of `chunk_days`."""
    if chunk_days < 1:
        raise ValueError("chunk_days must be at least 1")
    # Built from a count rather than a while loop: this runs unattended against
    # a link that drops, and a chunker that fails to advance would hang the
    # fetch instead of failing it.  Enumerating the offsets makes that
    # impossible rather than merely unlikely.
    span = (end - start).days + 1
    return [
        (
            start + timedelta(days=offset),
            min(start + timedelta(days=offset + chunk_days - 1), end),
        )
        for offset in range(0, max(span, 0), chunk_days)
    ]


def monthly_counts(frame: pd.DataFrame, column: str = "trade_date") -> pd.Series:
    """Row count per calendar month, keyed 'YYYY-MM'."""
    months = pd.to_datetime(pd.Series(frame[column])).dt.strftime("%Y-%m")
    return months.groupby(months).size().sort_index()


def reconcile(local: pd.Series, remote: pd.Series) -> pd.DataFrame:
    """Months where the dump and the database disagree, with the signed gap.

    A month on one side only counts as zero on the other, so an extra month in
    the dump is as visible as a short one.
    """
    months = sorted(set(local.index) | set(remote.index))
    rows = []
    for month in months:
        mine = int(local.get(month, 0))
        theirs = int(remote.get(month, 0))
        if mine != theirs:
            rows.append(
                {"month": month, "dump_rows": mine, "db_rows": theirs, "delta": mine - theirs}
            )
    return pd.DataFrame(rows, columns=["month", "dump_rows", "db_rows", "delta"])
