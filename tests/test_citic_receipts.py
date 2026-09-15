"""Resolving what an absent warehouse receipt day means.

The design doc's §3: a day a receipt series omits usually *is* a zero, because
DCE writes one zero and then stops emitting rows.  But not always -- RU on SHFE
has three absent days bracketed by 26,500, and five DCE products carry genuine
gaps of their own.  So the rule reads the observation preceding the gap rather
than the exchange it came from.
"""

from datetime import date

import pandas as pd
import pytest

from citic_index.receipts import fill_absent_zeros


def _cal(start="2024-01-01", periods=10):
    return [d.date() for d in pd.bdate_range(start, periods=periods)]


def _obs(pairs, *, product="EG"):
    """pairs: [(day_index, value)] against the shared calendar."""
    cal = _cal()
    return pd.DataFrame(
        {
            "trade_date": [cal[i] for i, _ in pairs],
            "product": product,
            "receipts": [v for _, v in pairs],
        }
    )


def _filled(pairs, **kw):
    out = fill_absent_zeros(_obs(pairs), _cal(), **kw)
    return out.set_index("trade_date")["receipts"]


def test_a_gap_after_a_zero_is_filled_with_zeros():
    # DCE's shape: one zero, then silence until receipts return.
    s = _filled([(0, 100.0), (1, 0.0), (5, 40.0)])
    assert s[_cal()[2]] == 0.0
    assert s[_cal()[3]] == 0.0
    assert s[_cal()[4]] == 0.0


def test_a_gap_after_a_nonzero_stays_missing():
    # RU's shape: 26,500 on both sides of the hole.  Filling would fabricate a
    # collapse.
    s = _filled([(0, 26500.0), (1, 26500.0), (5, 25775.0)])
    assert s[_cal()[2] : _cal()[4]].isna().all()


def test_the_run_before_the_gap_decides_not_the_product():
    # Same product, two gaps: one opens after a zero, one after a value.
    s = _filled([(0, 10.0), (1, 0.0), (3, 50.0), (6, 70.0)])
    assert s[_cal()[2]] == 0.0  # after the zero
    assert pd.isna(s[_cal()[4]])  # after 50.0
    assert pd.isna(s[_cal()[5]])


def test_days_before_the_first_observation_are_never_filled():
    """The product had not listed; a zero there would hand it history it never
    lived through.

    Asserting those days are NaN is not enough -- a frame that covered them and
    left them empty would pass that too, and did (mutation M4).  They must not
    be in the frame at all.
    """
    out = fill_absent_zeros(_obs([(4, 0.0), (5, 0.0), (7, 10.0)]), _cal())
    assert out["trade_date"].min() == _cal()[4]


def test_the_tail_after_a_final_zero_is_filled_through_the_cutoff():
    # JD's shape on 2026-08-05: a final zero, then nothing, while every other
    # product runs to the cutoff.  Without this it leaves the cross-section.
    out = fill_absent_zeros(_obs([(0, 5.0), (1, 0.0)]), _cal(), through=_cal()[4])
    s = out.set_index("trade_date")["receipts"]
    assert (s[_cal()[1] : _cal()[4]] == 0.0).all()
    assert out["trade_date"].max() == _cal()[4]  # the cutoff is a ceiling, not a floor


def test_the_tail_after_a_final_nonzero_stays_missing():
    # A series that simply stopped updating is not a series of zeros.
    s = _filled([(0, 5.0), (1, 900.0)], through=_cal()[4])
    assert s[_cal()[2] : _cal()[4]].isna().all()


def test_a_cutoff_past_the_calendar_stops_at_the_calendar():
    # The calendar is the authority on which days exist; a cutoff beyond it
    # must not invent days past its end.
    out = fill_absent_zeros(
        _obs([(0, 5.0), (1, 0.0)]), _cal(), through=date(2030, 1, 1)
    )
    assert out["trade_date"].max() == _cal()[-1]


def test_without_a_cutoff_the_tail_is_left_alone():
    # No cutoff means no opinion about days past the last observation: the
    # frame simply ends there rather than emitting zeros to the calendar's end.
    out = fill_absent_zeros(_obs([(0, 5.0), (1, 0.0)]), _cal())
    assert out["trade_date"].max() == _cal()[1]


def test_non_trading_days_never_appear():
    cal = _cal()
    obs = _obs([(0, 10.0), (4, 20.0)])
    out = fill_absent_zeros(obs, cal)
    assert set(out["trade_date"]) <= set(cal)
    # the weekend between index 4 and 5 of a bdate_range is already excluded
    assert len(out) == len(cal[: cal.index(obs["trade_date"].max()) + 1])


def test_each_product_is_resolved_on_its_own():
    a = _obs([(0, 100.0), (1, 0.0), (5, 40.0)], product="EG")
    b = _obs([(0, 26500.0), (1, 26500.0), (5, 25775.0)], product="RU")
    out = fill_absent_zeros(pd.concat([a, b], ignore_index=True), _cal())
    eg = out[out["product"] == "EG"].set_index("trade_date")["receipts"]
    ru = out[out["product"] == "RU"].set_index("trade_date")["receipts"]
    assert eg[_cal()[3]] == 0.0
    assert pd.isna(ru[_cal()[3]])


def test_observed_zeros_are_left_as_observed():
    s = _filled([(0, 0.0), (1, 0.0), (2, 10.0)])
    assert s[_cal()[0]] == 0.0
    assert s[_cal()[1]] == 0.0


def test_the_report_counts_what_it_did():
    out = fill_absent_zeros(
        _obs([(0, 100.0), (1, 0.0), (5, 40.0)]), _cal(), with_report=True
    )
    frame, report = out
    assert report["filled_zero"] == 3
    assert report["left_missing"] == 0
    frame2, report2 = fill_absent_zeros(
        _obs([(0, 26500.0), (1, 26500.0), (5, 25775.0)]), _cal(), with_report=True
    )
    assert report2["filled_zero"] == 0
    assert report2["left_missing"] == 3


def test_an_empty_frame_is_returned_empty():
    out = fill_absent_zeros(
        pd.DataFrame(columns=["trade_date", "product", "receipts"]), _cal()
    )
    assert out.empty
    assert list(out.columns) == ["trade_date", "product", "receipts"]


def test_a_duplicate_product_day_is_refused():
    cal = _cal()
    dup = pd.DataFrame(
        {
            "trade_date": [cal[0], cal[0]],
            "product": "EG",
            "receipts": [1.0, 2.0],
        }
    )
    # Match our own prefix: pandas raises "cannot reindex on an axis with
    # duplicate labels" for the same input, and a bare match="duplicate" was
    # satisfied by that instead of by the check (mutation M5).
    with pytest.raises(ValueError, match="citic_receipts: duplicate"):
        fill_absent_zeros(dup, cal)
