from datetime import date

import pandas as pd
import pytest

from citic_index.factor import basis_momentum


def _legs(t1_returns, t2_returns, *, month_gap=4, product="M", start="2024-01-01"):
    days = pd.bdate_range(start, periods=len(t1_returns))
    return pd.DataFrame(
        {
            "trade_date": [d.date() for d in days],
            "product": product,
            "t1_return": t1_returns,
            "t2_return": t2_returns,
            "month_gap": month_gap,
        }
    )


def test_bm_is_the_product_difference_divided_by_the_month_gap():
    # T1 gains 10% three days running, T2 is flat.  1.1**3 = 1.331 against
    # 1.0, so the raw difference is 0.331; over a four-month gap, 0.08275.
    frame = _legs([0.10, 0.10, 0.10], [0.0, 0.0, 0.0], month_gap=4)
    out = basis_momentum(frame, window=3, min_observations=3, normalise_by_gap=True)
    assert out["basis_momentum"].iloc[-1] == pytest.approx(0.08275, abs=1e-12)


def test_without_normalisation_it_is_the_raw_product_difference():
    # Deviation 2 in the design doc: the shipped leg skips the division.
    frame = _legs([0.10, 0.10, 0.10], [0.0, 0.0, 0.0], month_gap=4)
    out = basis_momentum(frame, window=3, min_observations=3, normalise_by_gap=False)
    assert out["basis_momentum"].iloc[-1] == pytest.approx(0.331, abs=1e-12)


def test_the_month_gap_of_the_signal_day_is_the_one_that_divides():
    # The gap moves when a leg rolls.  The factor is dated at t, so the gap
    # standing at t is the one that applies -- not the one the window opened on.
    frame = _legs([0.10, 0.10, 0.10], [0.0, 0.0, 0.0], month_gap=4)
    frame.loc[frame.index[-1], "month_gap"] = 2
    out = basis_momentum(frame, window=3, min_observations=3, normalise_by_gap=True)
    assert out["basis_momentum"].iloc[-1] == pytest.approx(0.1655, abs=1e-12)


def test_the_window_truncates_rather_than_accumulating_everything():
    # R is the one free parameter the methodology leaves unset, so the window
    # has to actually bite.  Five days, window of three: only the last three
    # count, 1.1**3 - 1 = 0.331 over a four-month gap.  Were the window ignored
    # the two 50% days would still be in there and the answer would be 0.4987.
    frame = _legs([0.50, 0.50, 0.10, 0.10, 0.10], [0.0] * 5, month_gap=4)
    out = basis_momentum(frame, window=3, min_observations=3, normalise_by_gap=True)
    assert out["basis_momentum"].iloc[-1] == pytest.approx(0.08275, abs=1e-12)
    assert out["bm_observations"].iloc[-1] == 3


def test_a_short_window_is_refused_rather_than_padded():
    # Padding a young product's window with zero returns hands it a lookback it
    # has not lived through.  Two observations under a three-observation floor
    # produce nothing at all.
    frame = _legs([0.10, 0.10], [0.0, 0.0])
    out = basis_momentum(frame, window=3, min_observations=3, normalise_by_gap=True)
    assert out["basis_momentum"].isna().all()
    assert not out["bm_ready"].any()


def test_a_missing_day_is_skipped_not_treated_as_a_zero_return():
    # A day the chain could not price is absent, not zero.  Two real 10% days
    # under a floor of two give 1.1**2 - 1 = 0.21, over a gap of four: 0.0525.
    frame = _legs([0.10, float("nan"), 0.10], [0.0, float("nan"), 0.0], month_gap=4)
    out = basis_momentum(frame, window=3, min_observations=2, normalise_by_gap=True)
    assert out["basis_momentum"].iloc[-1] == pytest.approx(0.0525, abs=1e-12)
    # Were the hole read as a zero return the count would reach three and the
    # value would be unchanged -- so assert the count itself.
    assert out["bm_observations"].iloc[-1] == 2


def test_a_day_only_one_leg_could_price_is_dropped_from_both():
    # T1 priced on all three days, T2 only on two.  Differencing cumulative
    # returns taken over different day sets is not a like-for-like comparison,
    # so the day T2 is missing leaves T1's window too: 1.1**2 - 1 = 0.21 over a
    # four-month gap is 0.0525, not the 0.08275 all three days would give.
    frame = _legs([0.10, 0.10, 0.10], [0.0, float("nan"), 0.0], month_gap=4)
    out = basis_momentum(frame, window=3, min_observations=2, normalise_by_gap=True)
    assert out["basis_momentum"].iloc[-1] == pytest.approx(0.0525, abs=1e-12)
    assert out["bm_observations"].iloc[-1] == 2
    assert out["t1_cumulative"].iloc[-1] == pytest.approx(0.21, abs=1e-12)


def test_each_product_gets_its_own_window():
    m = _legs([0.10, 0.10, 0.10], [0.0, 0.0, 0.0], month_gap=4, product="M")
    y = _legs([0.0, 0.0, 0.0], [0.10, 0.10, 0.10], month_gap=4, product="Y")
    out = basis_momentum(
        pd.concat([m, y], ignore_index=True),
        window=3,
        min_observations=3,
        normalise_by_gap=True,
    )
    last = out.loc[out["trade_date"] == date(2024, 1, 3)].set_index("product")
    assert last.loc["M", "basis_momentum"] == pytest.approx(0.08275, abs=1e-12)
    assert last.loc["Y", "basis_momentum"] == pytest.approx(-0.08275, abs=1e-12)
