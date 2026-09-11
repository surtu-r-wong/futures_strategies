from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from citic_index.book import lever, next_targets, rolling_vol_scale
from citic_index.pipeline import ReplicaConfig


def _days(n, start=date(2020, 1, 1)):
    return [start + timedelta(days=i) for i in range(n)]


def test_the_scale_is_the_target_over_the_realised_vol():
    # A constant 1% daily move has an annualised vol of 0.01*sqrt(252)=15.87%,
    # so a 15% target asks for 15/15.87 = 0.9449 of it.
    days = _days(300)
    returns = pd.Series([0.01, -0.01] * 150, index=days)
    scale = rolling_vol_scale(returns, vol_window=252, target_vol=0.15, min_observations=252)
    assert scale.iloc[-1] == pytest.approx(0.15 / (0.01 * np.sqrt(252.0)), rel=1e-9)


def test_the_scale_is_undefined_until_the_window_is_full():
    days = _days(300)
    returns = pd.Series([0.01, -0.01] * 150, index=days)
    scale = rolling_vol_scale(returns, vol_window=252, target_vol=0.15, min_observations=252)
    # 251 days is not a window.  Sizing a live book off a short one reads the
    # vol wrong and levers against it, which is the defect that shipped in the
    # carry runner on 2026-09-11.
    assert scale.iloc[:251].isna().all()
    assert scale.iloc[251:].notna().all()


def test_the_scale_never_uses_a_return_it_could_not_have_seen():
    # The window ending at T must close at T.  A scale that reached into T+1
    # would size today's book off tomorrow's move.
    days = _days(300)
    # Alternating rather than constant: a flat series has zero standard
    # deviation, so the scale is undefined and the test would prove nothing.
    calm = [0.001, -0.001] * 149 + [0.001]
    returns = pd.Series(calm + [0.50], index=days)
    scale = rolling_vol_scale(returns, vol_window=252, target_vol=0.15, min_observations=252)
    quiet = 0.15 / (0.001 * np.sqrt(252.0))
    assert scale.iloc[-2] == pytest.approx(quiet, rel=1e-9)
    assert scale.iloc[-1] < quiet   # the shock is in the window only on its own day


def test_levering_multiplies_the_weights_and_reports_the_scale():
    weights = pd.DataFrame(
        {
            "trade_date": [date(2020, 1, 1)] * 2,
            "product": ["M", "Y"],
            "weight": [-0.2, 0.2],
        }
    )
    scale = pd.Series({date(2020, 1, 1): 3.0})
    out = lever(weights, scale, config=ReplicaConfig())
    assert out.set_index("product")["target_weight"].to_dict() == pytest.approx(
        {"M": -0.6, "Y": 0.6}
    )
    assert out["vol_scale"].unique().tolist() == [3.0]


def test_the_gross_cap_binds_proportionally():
    # Four products at 0.25 gross apiece is a gross of 1.0; a scale of 10 would
    # ask for 10.0, and the cap holds it at 4.0 while keeping the shape.
    weights = pd.DataFrame(
        {
            "trade_date": [date(2020, 1, 1)] * 4,
            "product": list("ABCD"),
            "weight": [-0.4, -0.1, 0.1, 0.4],
        }
    )
    scale = pd.Series({date(2020, 1, 1): 10.0})
    out = lever(weights, scale, config=ReplicaConfig(max_gross_leverage=4.0))
    target = out.set_index("product")["target_weight"]
    assert target.abs().sum() == pytest.approx(4.0)
    # Shape preserved: the ratios between the legs do not move.
    assert target["D"] / target["A"] == pytest.approx(-1.0)
    assert target["D"] / target["C"] == pytest.approx(4.0)


def test_a_day_with_no_scale_yet_produces_no_targets():
    weights = pd.DataFrame(
        {"trade_date": [date(2020, 1, 1)], "product": ["M"], "weight": [0.2]}
    )
    out = lever(weights, pd.Series({date(2020, 1, 1): float("nan")}), config=ReplicaConfig())
    assert out.empty


# --- the order sheet ---------------------------------------------------------

LEGS = pd.DataFrame(
    {
        "trade_date": [date(2020, 1, 2)] * 2,
        "product": ["M", "Y"],
        "main_contract": ["M2005.DCE", "Y2005.DCE"],
        "main_close": [3000.0, 6000.0],
    }
)
POOL = pd.DataFrame(
    {
        "trade_date": [date(2020, 1, 2)] * 2,
        "product": ["M", "Y"],
        "multiplier": [10.0, 10.0],
    }
)


def _levered(weights):
    return pd.DataFrame(
        {
            "trade_date": [date(2020, 1, 2)] * len(weights),
            "product": list(weights),
            "weight": [w / 3.0 for w in weights.values()],
            "vol_scale": 3.0,
            "target_weight": list(weights.values()),
        }
    )


def test_lots_are_the_notional_over_the_contract_value():
    # M: 0.30 * 1e8 = 30,000,000 over 3000*10 = 1000 -> 1000 lots exactly.
    # Y: -0.12 * 1e8 = -12,000,000 over 6000*10 = 200 -> -200 lots.
    out = next_targets(
        _levered({"M": 0.30, "Y": -0.12}), LEGS, POOL,
        signal_date=date(2020, 1, 2), capital=1e8,
    )
    lots = out.set_index("product")["lots"]
    assert lots["M"] == 1000
    assert lots["Y"] == -200
    assert out.set_index("product")["order_code"]["M"] == "m2005"


def test_the_direction_column_follows_the_sign_of_the_target():
    out = next_targets(
        _levered({"M": 0.30, "Y": -0.12}), LEGS, POOL,
        signal_date=date(2020, 1, 2), capital=1e8,
    )
    assert out.set_index("product")["direction"].to_dict() == {"M": 1, "Y": -1}


def test_a_product_whose_contract_has_no_bar_is_refused_not_dropped():
    # Silently dropping it would send a sheet that is short one leg and still
    # looks complete.  The carry runner refuses the whole sheet here and so
    # does this one.
    legs = LEGS.loc[LEGS["product"] == "M"]
    with pytest.raises(ValueError, match="Y"):
        next_targets(
            _levered({"M": 0.30, "Y": -0.12}), legs, POOL,
            signal_date=date(2020, 1, 2), capital=1e8,
        )


def test_a_product_with_no_multiplier_is_refused():
    pool = POOL.loc[POOL["product"] == "M"]
    with pytest.raises(ValueError, match="Y"):
        next_targets(
            _levered({"M": 0.30, "Y": -0.12}), LEGS, pool,
            signal_date=date(2020, 1, 2), capital=1e8,
        )


def test_only_the_signal_date_is_emitted():
    levered = pd.concat(
        [
            _levered({"M": 0.30, "Y": -0.12}),
            _levered({"M": 0.10, "Y": -0.10}).assign(trade_date=date(2020, 1, 3)),
        ],
        ignore_index=True,
    )
    out = next_targets(
        levered, LEGS, POOL, signal_date=date(2020, 1, 2), capital=1e8
    )
    assert out["signal_date"].unique().tolist() == [date(2020, 1, 2)]
    assert len(out) == 2
