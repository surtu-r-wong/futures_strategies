from datetime import date

import pandas as pd
import pytest

from citic_index.factor import term_structure


def _legs(t1_closes, t2_closes, *, month_gap=4, product="M"):
    days = pd.bdate_range("2024-01-01", periods=len(t1_closes))
    return pd.DataFrame(
        {
            "trade_date": [d.date() for d in days],
            "product": product,
            "t1_close": t1_closes,
            "t2_close": t2_closes,
            "month_gap": month_gap,
        }
    )


def test_the_roll_yield_is_the_near_minus_far_over_near_over_the_gap():
    # CITIC 025 3.5 step 1: (P_near - P_far) / P_near / months apart.
    # (110 - 100) / 110 / 4 = 0.0227272...
    frame = _legs([110.0], [100.0], month_gap=4)
    out = term_structure(frame, lookback=1, min_observations=1)
    assert out["term_structure"].iloc[0] == pytest.approx(10 / 110 / 4)


def test_backwardation_is_positive_and_contango_negative():
    # Near above far is backwardation and pays the roll; near below far is
    # contango and costs it.  The sign has to carry that, because step 2 ranks
    # ascending and the top of the ranking is the long leg.
    out = term_structure(_legs([110.0, 90.0], [100.0, 100.0]), lookback=1, min_observations=1)
    assert out["term_structure"].iloc[0] > 0
    assert out["term_structure"].iloc[1] < 0


def test_the_factor_is_the_arithmetic_mean_over_the_lookback():
    # 3.5 step 2: "求出 p 日(参数)展期收益率 R_i 的平均值 meanR_i".  Three days of
    # 0.10, 0.20, 0.30 (before the gap divide) average to 0.20.
    frame = _legs([100.0, 100.0, 100.0], [90.0, 80.0, 70.0], month_gap=1)
    out = term_structure(frame, lookback=3, min_observations=3)
    # raw = (100-90)/100/1 = 0.10, then 0.20, then 0.30 -> mean 0.20
    assert out["term_structure"].iloc[-1] == pytest.approx(0.20)


def test_the_lookback_truncates():
    frame = _legs([100.0] * 5, [50.0, 50.0, 90.0, 80.0, 70.0], month_gap=1)
    # raw = 0.5, 0.5, 0.10, 0.20, 0.30.  Over the last three: mean 0.20.
    out = term_structure(frame, lookback=3, min_observations=3)
    assert out["term_structure"].iloc[-1] == pytest.approx(0.20)


def test_a_short_window_is_refused_rather_than_padded():
    frame = _legs([100.0, 100.0], [90.0, 80.0], month_gap=1)
    out = term_structure(frame, lookback=3, min_observations=3)
    assert out["term_structure"].isna().all()
    assert not out["ts_ready"].any()


def test_each_product_gets_its_own_window():
    m = _legs([100.0] * 3, [90.0] * 3, month_gap=1, product="M")
    y = _legs([100.0] * 3, [110.0] * 3, month_gap=1, product="Y")
    out = term_structure(pd.concat([m, y], ignore_index=True), lookback=3, min_observations=3)
    last = out.loc[out["trade_date"] == date(2024, 1, 3)].set_index("product")
    assert last.loc["M", "term_structure"] == pytest.approx(0.10)
    assert last.loc["Y", "term_structure"] == pytest.approx(-0.10)
