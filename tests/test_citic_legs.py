from datetime import date

import pandas as pd

from citic_index.legs import select_legs


def _day(contracts, *, day="2024-03-01", product="M"):
    """contracts: (code, delivery_yyyymm, oi, close) tuples for one product-day."""
    return pd.DataFrame(
        {
            "trade_date": [date.fromisoformat(day)] * len(contracts),
            "product": product,
            "contract": [c[0] for c in contracts],
            "delivery_yyyymm": [c[1] for c in contracts],
            "oi": [float(c[2]) for c in contracts],
            "close": [float(c[3]) for c in contracts],
        }
    )


def test_t1_is_the_highest_oi_contract_delivering_before_the_dominant():
    # CITIC 3.5 step 1. M2405 holds the most open interest, so it is the
    # dominant; M2403 is the only contract delivering before it.
    frame = _day(
        [
            ("M2403.DCE", 202403, 30000, 3100.0),
            ("M2405.DCE", 202405, 90000, 3000.0),
            ("M2409.DCE", 202409, 50000, 2950.0),
        ]
    )
    row = select_legs(frame).iloc[0]
    assert row["main_contract"] == "M2405.DCE"
    assert row["t1_contract"] == "M2403.DCE"
    assert row["t2_contract"] == "M2409.DCE"
    assert row["month_gap"] == 6  # 202403 -> 202409


def test_t1_falls_back_to_the_dominant_when_nothing_delivers_earlier():
    frame = _day(
        [
            ("M2405.DCE", 202405, 90000, 3000.0),
            ("M2409.DCE", 202409, 50000, 2950.0),
        ]
    )
    row = select_legs(frame).iloc[0]
    assert row["t1_contract"] == "M2405.DCE"
    assert row["month_gap"] == 4  # 202405 -> 202409


def test_t1_takes_open_interest_not_nearness_among_the_earlier_contracts():
    # 3.5 step 1 says "持仓最大的合约".  M2403 sits nearest to the dominant but
    # M2401 holds three times its open interest, so the two rules disagree here
    # and open interest has to win.
    frame = _day(
        [
            ("M2401.DCE", 202401, 30000, 3200.0),
            ("M2403.DCE", 202403, 10000, 3100.0),
            ("M2405.DCE", 202405, 90000, 3000.0),
            ("M2409.DCE", 202409, 50000, 2950.0),
        ]
    )
    row = select_legs(frame).iloc[0]
    assert row["t1_contract"] == "M2401.DCE"
    assert row["month_gap"] == 8  # 202401 -> 202409


def test_t2_takes_open_interest_not_nearness_among_the_later_contracts():
    # M2407 is the nearest later month but M2409 holds more open interest.
    frame = _day(
        [
            ("M2403.DCE", 202403, 30000, 3100.0),
            ("M2405.DCE", 202405, 90000, 3000.0),
            ("M2407.DCE", 202407, 20000, 2980.0),
            ("M2409.DCE", 202409, 50000, 2950.0),
        ]
    )
    row = select_legs(frame).iloc[0]
    assert row["t2_contract"] == "M2409.DCE"
    assert row["month_gap"] == 6


def test_the_main_switch_reproduces_the_shipped_leg():
    # Deviation 1 in the design doc: the shipped basis-momentum leg feeds the
    # chains the dominant contract, not the near dominant.  Kept as a switch so
    # the attribution task can price the difference.
    frame = _day(
        [
            ("M2403.DCE", 202403, 30000, 3100.0),
            ("M2405.DCE", 202405, 90000, 3000.0),
            ("M2409.DCE", 202409, 50000, 2950.0),
        ]
    )
    row = select_legs(frame, t1_leg="main").iloc[0]
    assert row["t1_contract"] == "M2405.DCE"
    assert row["month_gap"] == 4  # 202405 -> 202409, not 6


def test_a_product_with_no_later_contract_is_dropped():
    frame = _day(
        [
            ("M2403.DCE", 202403, 30000, 3100.0),
            ("M2405.DCE", 202405, 90000, 3000.0),
        ]
    )
    assert select_legs(frame).empty


def test_every_product_day_gets_its_own_row():
    frames = [
        _day(
            [
                ("M2403.DCE", 202403, 30000, 3100.0),
                ("M2405.DCE", 202405, 90000, 3000.0),
                ("M2409.DCE", 202409, 50000, 2950.0),
            ],
            day=day,
        )
        for day in ("2024-03-01", "2024-03-04")
    ]
    frames.append(
        _day(
            [
                ("Y2405.DCE", 202405, 40000, 9000.0),
                ("Y2409.DCE", 202409, 20000, 8900.0),
            ],
            day="2024-03-01",
            product="Y",
        )
    )
    legs = select_legs(pd.concat(frames, ignore_index=True))
    assert len(legs) == 3
    # Panel order: trade date first, then product, so a day is one contiguous
    # cross-section -- which is what the ranking step consumes.
    assert list(zip(legs["trade_date"], legs["product"])) == [
        (date(2024, 3, 1), "M"),
        (date(2024, 3, 1), "Y"),
        (date(2024, 3, 4), "M"),
    ]
    assert legs.loc[legs["product"] == "Y", "t1_contract"].item() == "Y2405.DCE"
