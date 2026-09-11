from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from citic_index.compare import align, compare, lag_scan, render, yearly


RETURNS = [0.01, -0.02, 0.03, -0.01, 0.02, 0.015, -0.025, 0.005, 0.012, -0.008]


def _levels(returns, *, base=1000.0):
    level = base
    out = [base]
    for r in returns:
        level *= 1.0 + r
        out.append(level)
    return out


def _days(n, start=date(2010, 1, 4)):
    return [start + timedelta(days=i) for i in range(n)]


def _official(returns, code="CICSF027.WI"):
    levels = _levels(returns)
    return pd.DataFrame(
        {"index_code": code, "trade_date": _days(len(levels)), "close": levels}
    )


def _replica(returns):
    levels = _levels(returns)
    return pd.DataFrame({"trade_date": _days(len(levels)), "index_value": levels})


def test_align_keeps_only_the_shared_days():
    official = _official(RETURNS)
    replica = _replica(RETURNS).iloc[2:]
    out = align(replica, official, code="CICSF027.WI")
    assert len(out) == len(RETURNS) + 1 - 2
    assert out["trade_date"].iloc[0] == _days(11)[2]


def test_a_perfect_replica_correlates_at_one_on_lag_zero():
    out = compare(_replica(RETURNS), _official(RETURNS), code="CICSF027.WI")
    assert out["correlation"] == pytest.approx(1.0)
    assert out["best_lag"] == 0


def test_a_one_day_slip_shows_up_in_the_lag_scan_not_in_the_headline():
    # The replica is the official series one day late: identical strategy,
    # wrong alignment.  Reading only the lag-zero number would call this a
    # failed replication -- which is exactly what happened to the 025
    # cross-validation before it was lag-scanned.
    slipped = [0.0] + RETURNS[:-1]
    out = compare(_replica(slipped), _official(RETURNS), code="CICSF027.WI")
    # What matters is not that lag zero is near nothing -- an autocorrelated
    # series can read anything there -- but that the peak is somewhere else.
    assert out["best_lag"] == 1
    assert abs(out["correlation"]) < abs(out["best_correlation"]) - 0.3
    assert out["best_correlation"] == pytest.approx(1.0)
    assert "alignment, not specification" in render(out)


def test_the_scan_covers_both_directions():
    scan = lag_scan(align(_replica(RETURNS), _official(RETURNS), code="CICSF027.WI"))
    assert scan["lag"].tolist() == [-3, -2, -1, 0, 1, 2, 3]
    assert scan["n"].min() > 0


def test_yearly_puts_the_two_series_side_by_side():
    # Two calendar years, +10% then +21% for the replica against flat official.
    days = [date(2010, 12, 31), date(2011, 6, 30), date(2011, 12, 30)]
    replica = pd.DataFrame({"trade_date": days, "index_value": [1000.0, 1100.0, 1210.0]})
    official = pd.DataFrame(
        {"index_code": "X", "trade_date": days, "close": [1000.0, 1000.0, 1000.0]}
    )
    out = yearly(align(replica, official, code="X"))
    assert out.set_index("year").loc[2010, "replica"] == pytest.approx(0.0)
    # 2011 holds both moves: 1000 -> 1100 -> 1210 is +21%.
    assert out.set_index("year").loc[2011, "replica"] == pytest.approx(0.21)
    assert out.set_index("year").loc[2011, "gap"] == pytest.approx(0.21)


def test_performance_is_reported_for_both_series():
    out = compare(_replica(RETURNS), _official(RETURNS), code="CICSF027.WI")
    for key in ("replica_performance", "official_performance"):
        assert set(out[key]) == {"ann_return", "ann_vol", "sharpe", "max_drawdown", "days"}
        assert out[key]["days"] == len(RETURNS)
    # Identical series must report identical statistics.
    assert out["replica_performance"] == pytest.approx(out["official_performance"], nan_ok=True)


def test_an_unknown_index_code_is_refused_rather_than_silently_empty():
    with pytest.raises(ValueError, match="no official rows"):
        align(_replica(RETURNS), _official(RETURNS), code="CICSF999.WI")


def test_rank_correlation_survives_a_monotone_distortion():
    # Squaring the positive returns keeps their order but not their spacing, so
    # a rank correlation should stay at 1 where Pearson's falls away.
    distorted = [np.sign(r) * r * r * 50 for r in RETURNS]
    out = compare(_replica(distorted), _official(RETURNS), code="CICSF027.WI")
    assert out["rank_correlation"] == pytest.approx(1.0)
    assert out["correlation"] < 1.0
