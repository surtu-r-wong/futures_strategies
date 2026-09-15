"""CITIC 026's time-series momentum factor and its equal-volatility weights.

026 is structurally unlike 023/025/027, which is why it needs its own factor
*and* its own weight scheme:

- direction comes from each product's own momentum sign (§3.5 step 2), not from
  a cross-sectional rank;
- sizes come from equal-volatility allocation, `w_i sigma_i^2` equal across
  products and summing to one (§3.3), so the book is **not** zero-sum and
  carries net exposure.
"""

import numpy as np
import pandas as pd
import pytest

from citic_index.factor import time_series_momentum
from citic_index.weights import equal_vol_weights


def _returns(series, *, product="M", start="2024-01-01"):
    days = pd.bdate_range(start, periods=len(series))
    return pd.DataFrame(
        {
            "trade_date": [d.date() for d in days],
            "product": product,
            "product_return": series,
        }
    )


# ---------------------------------------------------------------- the factor


def test_the_momentum_is_the_compounded_return_over_the_lookback():
    # 3.5 step 1: r_k = prod(1 + r_nk).  1.21 * 1.00 * 1.10 = 1.331.
    # The returns have to vary: a constant series has zero rolling volatility,
    # and equal-volatility sizing cannot divide by that, so the factor's own
    # gate refuses the day.
    out = time_series_momentum(_returns([0.21, 0.0, 0.10]), lookback=3, vol_window=2)
    assert out["ts_momentum"].iloc[-1] == pytest.approx(0.331, abs=1e-12)


def test_the_direction_is_the_sign_of_that_momentum():
    up = time_series_momentum(_returns([0.21, 0.0, 0.10]), lookback=3, vol_window=2)
    down = time_series_momentum(_returns([-0.21, 0.0, -0.10]), lookback=3, vol_window=2)
    assert up["direction"].iloc[-1] == 1
    assert down["direction"].iloc[-1] == -1


def test_a_flat_lookback_takes_no_position():
    # I(sum r > 0) is false at exactly zero, but holding a short on a product
    # that has not moved is an artefact; no position is the honest reading.
    out = time_series_momentum(
        _returns([0.10, -0.10 / 1.10, 0.0]), lookback=3, vol_window=2
    )
    assert out["ts_momentum"].iloc[-1] == pytest.approx(0.0, abs=1e-12)
    assert out["direction"].iloc[-1] == 0


def test_compounding_is_not_summation_when_returns_are_large():
    # 3.1 writes the factor as a sum of returns, 3.5 step 1 as a product.  They
    # agree in sign for small moves and can disagree for large ones, so the
    # operational section (3.5) is followed.  +50% then -40%: the sum is +10%
    # but the product is 1.5*0.6 = 0.9, a loss.
    out = time_series_momentum(_returns([0.50, -0.40]), lookback=2, vol_window=2)
    assert out["ts_momentum"].iloc[-1] == pytest.approx(-0.10, abs=1e-12)
    assert out["direction"].iloc[-1] == -1


def test_the_lookback_truncates_rather_than_accumulating_everything():
    out = time_series_momentum(
        _returns([9.0, 0.21, 0.0, 0.10]), lookback=3, vol_window=2
    )
    assert out["ts_momentum"].iloc[-1] == pytest.approx(0.331, abs=1e-12)


def test_a_short_history_reports_nothing():
    out = time_series_momentum(_returns([0.10, 0.10]), lookback=3, vol_window=2)
    assert not out["ts_ready"].any()


def test_the_volatility_window_is_its_own_parameter():
    """3.5 step 3 says "历史波动率定义为" and then never finishes the sentence,
    so the window is undefined in the document and has to be a parameter --
    even though 3.1 claims the strategy "只有一个参数即回望周期"."""
    values = [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.05, -0.05]
    short = time_series_momentum(_returns(values), lookback=2, vol_window=3)
    long = time_series_momentum(_returns(values), lookback=2, vol_window=6)
    assert short["sigma"].iloc[-1] != long["sigma"].iloc[-1]


def test_each_product_gets_its_own_windows():
    a = _returns([0.21, 0.0, 0.10], product="M")
    b = _returns([-0.21, 0.0, -0.10], product="Y")
    out = time_series_momentum(
        pd.concat([a, b], ignore_index=True), lookback=3, vol_window=2
    )
    last = out[out["ts_ready"]].set_index("product")
    assert last.loc["M", "direction"] == 1
    assert last.loc["Y", "direction"] == -1


def test_a_lookback_below_one_is_refused():
    with pytest.raises(ValueError, match="lookback"):
        time_series_momentum(_returns([0.1, 0.1]), lookback=0, vol_window=2)


# --------------------------------------------------------------- the weights


def _cross_section(sigmas, directions):
    return pd.DataFrame(
        {
            "product": list(sigmas),
            "sigma": [sigmas[p] for p in sigmas],
            "direction": [directions[p] for p in sigmas],
        }
    )


def test_equal_vol_makes_the_risk_contributions_equal():
    # 3.3: w_1 sigma_1^2 = ... = w_N sigma_N^2, and the weights sum to one.
    frame = _cross_section({"A": 0.01, "B": 0.02}, {"A": 1, "B": 1})
    w = equal_vol_weights(frame)
    contributions = w.to_numpy() * frame["sigma"].to_numpy() ** 2
    assert contributions[0] == pytest.approx(contributions[1], rel=1e-12)
    assert w.abs().sum() == pytest.approx(1.0, abs=1e-12)


def test_a_quieter_product_takes_a_bigger_weight():
    frame = _cross_section({"A": 0.01, "B": 0.02}, {"A": 1, "B": 1})
    w = equal_vol_weights(frame)
    # sigma doubles -> variance quadruples -> weight is a quarter
    assert w.iloc[0] == pytest.approx(4 * w.iloc[1], rel=1e-12)


def test_the_direction_signs_the_weight():
    frame = _cross_section({"A": 0.01, "B": 0.01}, {"A": 1, "B": -1})
    w = equal_vol_weights(frame)
    assert w.iloc[0] > 0 > w.iloc[1]
    assert w.abs().sum() == pytest.approx(1.0, abs=1e-12)


def test_the_book_is_not_zero_sum():
    """Unlike 023/025/027's rank weights, 026 carries net exposure: §3.3 fixes
    the *sizes* to sum to one and §3.5 step 2 signs them independently."""
    frame = _cross_section({"A": 0.01, "B": 0.01, "C": 0.01}, {"A": 1, "B": 1, "C": -1})
    w = equal_vol_weights(frame)
    assert w.sum() == pytest.approx(1 / 3, abs=1e-12)


def test_a_zero_direction_takes_no_weight_but_still_shares_the_budget():
    # A product with no position must not silently hand its budget to the
    # others: the sizes are set by volatility, the sign only orients them.
    frame = _cross_section({"A": 0.01, "B": 0.01}, {"A": 1, "B": 0})
    w = equal_vol_weights(frame)
    assert w.iloc[0] == pytest.approx(0.5, abs=1e-12)
    assert w.iloc[1] == pytest.approx(0.0, abs=1e-12)


def test_a_zero_volatility_product_is_refused_rather_than_taking_everything():
    frame = _cross_section({"A": 0.0, "B": 0.01}, {"A": 1, "B": 1})
    with pytest.raises(ValueError, match="sigma"):
        equal_vol_weights(frame)


def test_an_empty_cross_section_returns_empty():
    frame = pd.DataFrame(columns=["product", "sigma", "direction"])
    assert equal_vol_weights(frame).empty
