from datetime import date

import pandas as pd
import pytest

from citic_index.data import date_chunks, monthly_counts, reconcile


def test_chunks_tile_the_range_with_no_gap_and_no_overlap():
    chunks = date_chunks(date(2024, 1, 1), date(2024, 1, 10), chunk_days=4)
    assert chunks == [
        (date(2024, 1, 1), date(2024, 1, 4)),
        (date(2024, 1, 5), date(2024, 1, 8)),
        (date(2024, 1, 9), date(2024, 1, 10)),
    ]
    # Every day in the range belongs to exactly one chunk.
    covered = [
        day
        for lo, hi in chunks
        for day in pd.date_range(lo, hi).date
    ]
    assert covered == list(pd.date_range(date(2024, 1, 1), date(2024, 1, 10)).date)


def test_a_single_day_range_is_one_chunk():
    assert date_chunks(date(2024, 1, 1), date(2024, 1, 1), chunk_days=60) == [
        (date(2024, 1, 1), date(2024, 1, 1))
    ]


def test_an_empty_range_produces_nothing():
    assert date_chunks(date(2024, 1, 2), date(2024, 1, 1), chunk_days=60) == []


def test_a_non_positive_chunk_is_refused():
    # Zero would loop forever rather than fail, which is the worst outcome for
    # something meant to run unattended against a flaky link.
    with pytest.raises(ValueError, match="chunk_days"):
        date_chunks(date(2024, 1, 1), date(2024, 1, 10), chunk_days=0)


def test_monthly_counts_group_by_calendar_month():
    frame = pd.DataFrame(
        {
            "trade_date": [
                date(2024, 1, 31),
                date(2024, 2, 1),
                date(2024, 2, 2),
            ]
        }
    )
    counts = monthly_counts(frame)
    assert counts.to_dict() == {"2024-01": 1, "2024-02": 2}


def test_reconcile_is_empty_when_the_dump_matches_the_database():
    local = pd.Series({"2024-01": 100, "2024-02": 120})
    remote = pd.Series({"2024-01": 100, "2024-02": 120})
    assert reconcile(local, remote).empty


def test_reconcile_names_a_month_the_dump_is_short_on():
    local = pd.Series({"2024-01": 98, "2024-02": 120})
    remote = pd.Series({"2024-01": 100, "2024-02": 120})
    bad = reconcile(local, remote)
    assert bad["month"].tolist() == ["2024-01"]
    assert bad["delta"].tolist() == [-2]


def test_reconcile_names_a_month_the_dump_has_and_the_database_does_not():
    # A month present locally but absent upstream is just as wrong as a short
    # one, and treating a missing key as zero is what surfaces it.
    local = pd.Series({"2024-01": 100, "2024-03": 7})
    remote = pd.Series({"2024-01": 100})
    bad = reconcile(local, remote)
    assert bad["month"].tolist() == ["2024-03"]
    assert bad["delta"].tolist() == [7]
