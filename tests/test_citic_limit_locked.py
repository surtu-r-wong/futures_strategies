from datetime import date

import pandas as pd

from citic_index.legs import select_legs


def _day(rows, day="2024-03-01"):
    """rows: (contract, delivery, oi, close, high, low)."""
    return pd.DataFrame(
        {
            "trade_date": [date.fromisoformat(day)] * len(rows),
            "product": "M",
            "contract": [r[0] for r in rows],
            "delivery_yyyymm": [r[1] for r in rows],
            "oi": [float(r[2]) for r in rows],
            "close": [float(r[3]) for r in rows],
            "high": [float(r[4]) for r in rows],
            "low": [float(r[5]) for r in rows],
        }
    )


LEGS = [
    ("M2403.DCE", 202403, 30000, 3100.0, 3110.0, 3090.0),
    ("M2405.DCE", 202405, 90000, 3000.0, 3010.0, 2990.0),
    ("M2409.DCE", 202409, 50000, 2950.0, 2960.0, 2940.0),
]


def test_an_ordinary_day_is_not_limit_locked():
    row = select_legs(_day(LEGS)).iloc[0]
    assert bool(row["main_limit_locked"]) is False


def test_a_dominant_that_never_moved_is_limit_locked():
    # 3.2's special adjustment: a product limit-locked on the adjustment day is
    # out of the strategy, because there is no liquidity to take a position in.
    # A one-tick range on the dominant is what a locked board looks like in a
    # daily bar.
    locked = list(LEGS)
    locked[1] = ("M2405.DCE", 202405, 90000, 3000.0, 3000.0, 3000.0)
    row = select_legs(_day(locked)).iloc[0]
    assert bool(row["main_limit_locked"]) is True


def test_a_locked_non_dominant_contract_does_not_lock_the_product():
    # The index trades the dominant; a frozen far month says nothing about it.
    locked = list(LEGS)
    locked[2] = ("M2409.DCE", 202409, 50000, 2950.0, 2950.0, 2950.0)
    row = select_legs(_day(locked)).iloc[0]
    assert bool(row["main_limit_locked"]) is False


def test_bars_without_a_range_at_all_are_not_called_locked():
    # Missing high/low is missing data, not a locked board.
    unknown = [(c, d, o, cl, float("nan"), float("nan")) for c, d, o, cl, _, _ in LEGS]
    row = select_legs(_day(unknown)).iloc[0]
    assert bool(row["main_limit_locked"]) is False


# --- the ranking layer -------------------------------------------------------

import pytest  # noqa: E402

from citic_index.weights import assign_weights  # noqa: E402


def _factor(rows):
    """rows: (day, product, basis_momentum, limit_locked)."""
    return pd.DataFrame(
        {
            "trade_date": [date.fromisoformat(r[0]) for r in rows],
            "product": [r[1] for r in rows],
            "basis_momentum": [r[2] for r in rows],
            "bm_ready": True,
            "limit_locked": [r[3] for r in rows],
        }
    )


def test_a_locked_product_leaves_the_cross_section_and_shrinks_n():
    # 3.2: 排除在策略之外.  With B gone, N is 2 and the survivors take
    # (rank - 1.5)/3 = -1/6 and +1/6 rather than the -1/6, 0, +1/6 of N=3.
    out = assign_weights(
        _factor(
            [
                ("2024-03-01", "A", 1.0, False),
                ("2024-03-01", "B", 2.0, True),
                ("2024-03-01", "C", 3.0, False),
            ]
        )
    )
    assert set(out["product"]) == {"A", "C"}
    assert out["n_products"].unique().tolist() == [2]
    weights = out.set_index("product")["weight"]
    assert weights["A"] == pytest.approx(-1 / 6)
    assert weights["C"] == pytest.approx(1 / 6)


def test_a_lock_after_the_strike_does_not_unwind_a_monthly_position():
    # The exclusion is judged on the adjustment day.  A product that locks up
    # mid-month is one you cannot trade, not one you have stopped holding.
    out = assign_weights(
        _factor(
            [
                ("2024-02-01", "A", 1.0, False),
                ("2024-02-01", "B", 2.0, False),
                ("2024-02-01", "C", 3.0, False),
                ("2024-02-02", "A", 1.0, False),
                ("2024-02-02", "B", 2.0, True),
                ("2024-02-02", "C", 3.0, False),
            ]
        ),
        cadence="monthly",
    )
    held = out.loc[out["trade_date"] == date(2024, 2, 2)].set_index("product")
    assert set(held.index) == {"A", "B", "C"}
    assert held.loc["B", "weight"] == pytest.approx(0.0)   # struck on 02-01, held


def test_without_the_column_nothing_is_excluded():
    frame = _factor([("2024-03-01", "A", 1.0, False), ("2024-03-01", "B", 2.0, False)])
    out = assign_weights(frame.drop(columns=["limit_locked"]))
    assert set(out["product"]) == {"A", "B"}
