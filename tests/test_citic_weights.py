from datetime import date

import pandas as pd
import pytest

from citic_index.weights import assign_weights


def _factor(rows):
    """rows: (day, product, basis_momentum, bm_ready) tuples."""
    return pd.DataFrame(
        {
            "trade_date": [date.fromisoformat(r[0]) for r in rows],
            "product": [r[1] for r in rows],
            "basis_momentum": [r[2] for r in rows],
            "bm_ready": [r[3] for r in rows],
        }
    )


def _one_day(day, values):
    return _factor([(day, p, v, True) for p, v in values.items()])


def test_weights_are_the_centred_linear_rank_and_sum_to_zero():
    # CITIC 3.5 steps 2-3.  N=5, so w = (Rank - 3) / 15 over ranks 1..5.
    out = assign_weights(
        _one_day("2024-03-01", {"A": -2.0, "B": -1.0, "C": 0.0, "D": 1.0, "E": 2.0})
    )
    weights = out.set_index("product")["weight"]
    assert weights["A"] == pytest.approx(-2 / 15)
    assert weights["B"] == pytest.approx(-1 / 15)
    assert weights["C"] == pytest.approx(0.0)
    assert weights["D"] == pytest.approx(1 / 15)
    assert weights["E"] == pytest.approx(2 / 15)
    assert weights.sum() == pytest.approx(0.0, abs=1e-15)


def test_the_highest_basis_momentum_is_the_long_leg():
    # 3.5 step 2 sorts 从小到大, so the largest value takes the largest rank and
    # therefore the most positive weight.  Higher basis momentum predicts a
    # higher return on the near contract, so this is the sign the economics want.
    out = assign_weights(_one_day("2024-03-01", {"LOW": -0.5, "MID": 0.0, "HIGH": 0.5}))
    weights = out.set_index("product")["weight"]
    assert weights["HIGH"] > 0 > weights["LOW"]
    assert weights["HIGH"] == pytest.approx(1 / 6)   # (3 - 2) / 6


def test_products_that_failed_the_history_gate_are_out_of_the_cross_section():
    # N counts only the ready products, and N is in the weight formula, so a
    # product that cannot be ranked changes everyone else's weight.
    rows = [
        ("2024-03-01", "A", -2.0, True),
        ("2024-03-01", "B", -1.0, True),
        ("2024-03-01", "C", 0.0, True),
        ("2024-03-01", "D", 1.0, True),
        ("2024-03-01", "E", 2.0, True),
        ("2024-03-01", "F", 9.0, False),
        ("2024-03-01", "G", -9.0, False),
    ]
    out = assign_weights(_factor(rows))
    assert set(out["product"]) == {"A", "B", "C", "D", "E"}
    assert out["n_products"].unique().tolist() == [5]
    assert out.set_index("product").loc["E", "weight"] == pytest.approx(2 / 15)


def test_daily_cadence_reranks_every_day():
    frames = [
        _one_day("2024-02-01", {"A": 1.0, "B": 2.0, "C": 3.0}),
        _one_day("2024-02-02", {"A": 3.0, "B": 2.0, "C": 1.0}),
    ]
    out = assign_weights(pd.concat(frames, ignore_index=True), cadence="daily")
    second = out.loc[out["trade_date"] == date(2024, 2, 2)].set_index("product")
    assert second.loc["A", "weight"] == pytest.approx(1 / 6)
    assert second.loc["C", "weight"] == pytest.approx(-1 / 6)


def test_monthly_cadence_holds_the_struck_weights_inside_the_month():
    # Deviation 3 in the design doc: CITIC reranks every T+1, the shipped leg
    # strikes once a month.  Kept as a switch to price the difference.
    frames = [
        _one_day("2024-01-31", {"A": 1.0, "B": 2.0, "C": 3.0}),
        _one_day("2024-02-01", {"A": 3.0, "B": 2.0, "C": 1.0}),
        _one_day("2024-02-02", {"A": 1.0, "B": 2.0, "C": 3.0}),
    ]
    out = assign_weights(pd.concat(frames, ignore_index=True), cadence="monthly")
    held = out.loc[out["trade_date"] == date(2024, 2, 2)].set_index("product")
    # 02-01 is February's first trading day and struck A long; 02-02 keeps it
    # even though that day's own ranking would put A at the bottom.
    assert held.loc["A", "weight"] == pytest.approx(1 / 6)
    assert held.loc["C", "weight"] == pytest.approx(-1 / 6)


def test_monthly_recentres_when_a_product_leaves_the_cross_section():
    frames = [
        _one_day("2024-02-01", {"A": 1.0, "B": 2.0, "C": 3.0}),
        _one_day("2024-02-02", {"A": 1.0, "B": 2.0}),
    ]
    out = assign_weights(pd.concat(frames, ignore_index=True), cadence="monthly")
    held = out.loc[out["trade_date"] == date(2024, 2, 2)].set_index("product")
    # Struck weights were A -1/6, B 0, C +1/6.  C is gone, so the survivors are
    # recentred on their own mean of -1/12: A -1/12, B +1/12, still summing to 0.
    assert held.loc["A", "weight"] == pytest.approx(-1 / 12)
    assert held.loc["B", "weight"] == pytest.approx(1 / 12)
    assert held["weight"].sum() == pytest.approx(0.0, abs=1e-15)


def test_a_cross_section_too_small_to_rank_produces_nothing():
    out = assign_weights(_one_day("2024-03-01", {"A": 1.0}), min_products=2)
    assert out.empty


def test_an_unknown_cadence_is_refused():
    with pytest.raises(ValueError, match="cadence"):
        assign_weights(_one_day("2024-03-01", {"A": 1.0, "B": 2.0}), cadence="weekly")


def test_ties_resolve_by_product_name_whatever_the_input_order():
    # rank(method="first") breaks ties by row position, so the sort before it is
    # what keeps a tied cross-section from depending on how the rows arrived.
    values = {"B": 1.0, "A": 1.0, "C": 2.0}
    forward = assign_weights(_one_day("2024-03-01", values))
    reversed_rows = _one_day("2024-03-01", values).iloc[::-1].reset_index(drop=True)
    backward = assign_weights(reversed_rows)
    for out in (forward, backward):
        weights = out.set_index("product")["weight"]
        assert weights["A"] == pytest.approx(-1 / 6)   # rank 1 of 3
        assert weights["B"] == pytest.approx(0.0)      # rank 2
        assert weights["C"] == pytest.approx(1 / 6)    # rank 3
