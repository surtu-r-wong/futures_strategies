"""CITIC 023's warehouse receipt factor.

The paper's §3.1: `TS = C_p / C_{300-200} - 1`, where `C_p` averages the last p
trading days of standard warehouse receipts and `C_{300-200}` averages the
window running from 300 to 200 trading days back.
"""

import pandas as pd
import pytest

from citic_index.factor import warehouse_receipt


def _receipts(values, *, product="EG", start="2024-01-01"):
    days = pd.bdate_range(start, periods=len(values))
    return pd.DataFrame(
        {
            "trade_date": [d.date() for d in days],
            "product": product,
            "receipts": values,
        }
    )


def _factor(values, *, lookback=2, baseline_lag=4, baseline_window=2, **kw):
    """Small windows stand in for the paper's 200/100 so cases stay hand-checkable."""
    return warehouse_receipt(
        _receipts(values, **kw),
        lookback=lookback,
        baseline_lag=baseline_lag,
        baseline_window=baseline_window,
    )


def test_the_factor_is_the_recent_mean_over_the_baseline_mean_minus_one():
    # baseline = days at t-4 and t-3 -> (100+100)/2 = 100
    # recent   = days at t-1 and t   -> (150+250)/2 = 200
    # 200/100 - 1 = 1.0
    out = _factor([100, 100, 10, 10, 150, 250])
    assert out["warehouse_receipt"].iloc[-1] == pytest.approx(1.0, abs=1e-12)


def test_the_baseline_window_ends_at_the_lag_and_does_not_reach_the_recent_days():
    # Only the two baseline days change; the recent window is untouched, so a
    # factor that read the wrong offset would not move with them.
    base = _factor([100, 100, 10, 10, 150, 250])["warehouse_receipt"].iloc[-1]
    moved = _factor([400, 400, 10, 10, 150, 250])["warehouse_receipt"].iloc[-1]
    assert base == pytest.approx(1.0, abs=1e-12)
    assert moved == pytest.approx(-0.5, abs=1e-12)  # 200/400 - 1


def test_days_between_the_two_windows_are_ignored():
    # The 10s at t-2 sit in neither window; changing them must change nothing.
    a = _factor([100, 100, 10, 10, 150, 250])["warehouse_receipt"].iloc[-1]
    b = _factor([100, 100, 99999, 10, 150, 250])["warehouse_receipt"].iloc[-1]
    assert a == pytest.approx(b, abs=1e-12)


def test_a_zero_baseline_is_not_ranked_rather_than_dividing():
    # §3.1 has no zero guard and §3.2's special adjustments do not cover it.
    # House rule: no evidence, no coverage -- the product-day drops out.
    out = _factor([0, 0, 10, 10, 150, 250])
    assert bool(out["wr_ready"].iloc[-1]) is False
    assert pd.isna(out["warehouse_receipt"].iloc[-1])


def test_a_zero_in_the_recent_window_is_a_real_value_not_a_gap():
    # Receipts genuinely go to zero (mass cancellation).  That is signal --
    # the paper ranks it to the long end -- so it must average in, not drop out.
    out = _factor([100, 100, 10, 10, 0, 0])
    assert bool(out["wr_ready"].iloc[-1]) is True
    assert out["warehouse_receipt"].iloc[-1] == pytest.approx(-1.0, abs=1e-12)


def test_history_shorter_than_the_baseline_reports_nothing():
    out = _factor([100, 100, 150, 250])
    assert not out["wr_ready"].any()
    assert out["warehouse_receipt"].isna().all()


def test_the_first_day_that_can_be_computed_is_the_one_with_a_full_baseline():
    out = _factor([100, 100, 10, 10, 150, 250])
    ready = out.loc[out["wr_ready"], "trade_date"]
    assert len(ready) == 1
    assert ready.iloc[0] == out["trade_date"].iloc[-1]


def test_each_product_gets_its_own_windows():
    a = _receipts([100, 100, 10, 10, 150, 250], product="EG")
    b = _receipts([100, 100, 10, 10, 300, 500], product="JD")
    out = warehouse_receipt(
        pd.concat([a, b], ignore_index=True),
        lookback=2,
        baseline_lag=4,
        baseline_window=2,
    )
    last = out[out["wr_ready"]].set_index("product")["warehouse_receipt"]
    assert last["EG"] == pytest.approx(1.0, abs=1e-12)
    assert last["JD"] == pytest.approx(3.0, abs=1e-12)  # 400/100 - 1


def test_a_lookback_of_one_reads_the_day_itself():
    out = _factor([100, 100, 10, 10, 150, 250], lookback=1)
    assert out["warehouse_receipt"].iloc[-1] == pytest.approx(
        1.5, abs=1e-12
    )  # 250/100-1


def test_a_missing_receipt_blocks_the_day_rather_than_being_treated_as_zero():
    # Absence that survives into the factor layer is a genuine gap -- the
    # zero-filling rule runs upstream, in the receipts loader.  Here a NaN must
    # not quietly average as a zero.
    out = _factor([100, 100, 10, 10, float("nan"), 250])
    assert bool(out["wr_ready"].iloc[-1]) is False


def test_the_baseline_window_length_is_its_own_parameter():
    """lookback and baseline_window must not be confusable.

    Every other case here happens to set them equal, which makes a swap of one
    for the other an equivalent mutation -- caught by mutation testing, not by
    reading.  This one keeps them apart: with baseline_window=3 the baseline
    spans 400/100/100 (mean 200), and the factor is 200/200-1 = 0; a run that
    used lookback=2 for the baseline would span 100/100 and report 1.0.
    """
    out = warehouse_receipt(
        _receipts([999, 400, 100, 100, 999, 999, 150, 250]),
        lookback=2,
        baseline_lag=4,
        baseline_window=3,
    )
    assert out["warehouse_receipt"].iloc[-1] == pytest.approx(0.0, abs=1e-12)


def test_the_columns_are_the_declared_ones():
    from citic_index.factor import WAREHOUSE_RECEIPT_COLUMNS

    out = _factor([100, 100, 10, 10, 150, 250])
    assert tuple(out.columns) == WAREHOUSE_RECEIPT_COLUMNS


def test_a_lookback_below_one_is_refused():
    with pytest.raises(ValueError, match="lookback"):
        _factor([100, 100, 10, 10, 150, 250], lookback=0)


def test_an_empty_frame_returns_the_declared_columns():
    from citic_index.factor import WAREHOUSE_RECEIPT_COLUMNS

    out = warehouse_receipt(
        pd.DataFrame(columns=["trade_date", "product", "receipts"]),
        lookback=2,
        baseline_lag=4,
        baseline_window=2,
    )
    assert tuple(out.columns) == WAREHOUSE_RECEIPT_COLUMNS
    assert out.empty


def test_the_two_smoothing_targets_agree_while_the_baseline_stands_still():
    """023's §3.1 says the p-day mean is of 仓单数量 -- the level -- and §3.5 step 1
    repeats it as 仓单平均值/仓单平均值.  025's §3.1 averages the *factor* instead:
    "回望周期即将回望周期内的期限结构值算术平均值作为…因子值", which is what
    `term_structure` implements.

    With one constant baseline under both recent days the two coincide:
    mean(150,250)/100 - 1 = 1.0, and mean(150/100-1, 250/100-1) = 1.0.
    """
    values = [100, 100, 100, 100, 150, 250]
    kw = dict(lookback=2, baseline_lag=2, baseline_window=1)
    level = warehouse_receipt(_receipts(values), smoothing_target="level", **kw)
    ratio = warehouse_receipt(_receipts(values), smoothing_target="ratio", **kw)
    assert level["warehouse_receipt"].iloc[-1] == pytest.approx(1.0, abs=1e-12)
    assert ratio["warehouse_receipt"].iloc[-1] == pytest.approx(1.0, abs=1e-12)


def test_the_two_smoothing_targets_separate_once_the_baseline_moves():
    """Baselines 100 and 200 under the two recent days.

    level divides the averaged numerator by the baseline standing at t:
        mean(150,250) / 200 - 1 = 0.0
    ratio divides each day by its own baseline, then averages:
        mean(150/100 - 1, 250/200 - 1) = mean(0.5, 0.25) = 0.375

    023's document is demonstrably a rewrite of 025's -- it carries 025's 升贴水
    through a sentence whose every other term was changed to receipts -- so a
    rewrite of the code that kept 025's placement of the mean is a live
    possibility, and this is the shape it would take.
    """
    values = [999, 999, 100, 200, 150, 250]
    kw = dict(lookback=2, baseline_lag=2, baseline_window=1)
    level = warehouse_receipt(_receipts(values), smoothing_target="level", **kw)
    ratio = warehouse_receipt(_receipts(values), smoothing_target="ratio", **kw)
    assert level["warehouse_receipt"].iloc[-1] == pytest.approx(0.0, abs=1e-12)
    assert ratio["warehouse_receipt"].iloc[-1] == pytest.approx(0.375, abs=1e-12)


def test_an_unknown_smoothing_target_is_refused():
    with pytest.raises(ValueError, match="smoothing_target"):
        warehouse_receipt(
            _receipts([100, 100, 10, 10, 150, 250]),
            lookback=2,
            baseline_lag=4,
            baseline_window=2,
            smoothing_target="moon",
        )
