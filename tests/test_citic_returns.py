from datetime import date

import pandas as pd
import pytest

from citic_index.returns import chain_returns


def _prices(rows):
    """rows: (day, contract, close, settle)."""
    return pd.DataFrame(
        {
            "trade_date": [date.fromisoformat(r[0]) for r in rows],
            "contract": [r[1] for r in rows],
            "close": [r[2] for r in rows],
            "settle": [r[3] for r in rows],
        }
    )


def _chain(rows):
    return pd.DataFrame(
        {
            "trade_date": [date.fromisoformat(r[0]) for r in rows],
            "product": "M",
            "chain_contract": [r[1] for r in rows],
        }
    )


HELD = _chain([("2010-01-04", "M2001.DCE"), ("2010-01-05", "M2001.DCE")])
FLAT = _prices(
    [
        ("2010-01-04", "M2001.DCE", 100.0, 99.0),
        ("2010-01-05", "M2001.DCE", 110.0, 108.0),
    ]
)


def test_the_citic_basis_divides_by_the_previous_settlement():
    # 110 / 99 - 1 = 0.111111..., not 110/100 - 1 = 0.10.
    out = chain_returns(FLAT, HELD, basis="close_to_prev_settle")
    assert out["product_return"].iloc[0] == pytest.approx(110 / 99 - 1)


def test_the_tradeable_basis_divides_by_the_previous_close():
    out = chain_returns(FLAT, HELD, basis="close_to_close")
    assert out["product_return"].iloc[0] == pytest.approx(0.10)


def test_the_two_bases_differ_by_the_close_over_settle_ratio():
    # This is the untradeable accrual, one day of it: close(t-1)/settle(t-1).
    citic = chain_returns(FLAT, HELD, basis="close_to_prev_settle")["product_return"].iloc[0]
    tradeable = chain_returns(FLAT, HELD, basis="close_to_close")["product_return"].iloc[0]
    assert (1 + citic) / (1 + tradeable) == pytest.approx(100.0 / 99.0)


def test_an_unknown_basis_is_refused():
    with pytest.raises(ValueError, match="basis"):
        chain_returns(FLAT, HELD, basis="vwap")


def test_a_missing_settle_column_is_refused_rather_than_silently_using_close():
    with pytest.raises(ValueError, match="settle"):
        chain_returns(FLAT.drop(columns=["settle"]), HELD, basis="close_to_prev_settle")


def _roll_prices():
    # OLD 300 -> 312 on a settle of 300; NEW 100 -> 108 on a settle of 100, so
    # under the close-to-close basis the legs run +4% and +8%.
    return _prices(
        [
            ("2010-01-04", "M2001.DCE", 300.0, 300.0),
            ("2010-01-04", "M2005.DCE", 100.0, 100.0),
            ("2010-01-05", "M2001.DCE", 312.0, 312.0),
            ("2010-01-05", "M2005.DCE", 108.0, 108.0),
        ]
    )


ROLL = _chain([("2010-01-04", "M2001.DCE"), ("2010-01-05", "M2005.DCE")])


def test_a_roll_blends_the_legs_by_value_share_under_either_basis():
    # w_bar = 100 / (100 + 300) = 0.25 -> 0.25*0.08 + 0.75*0.04 = 0.05.
    for basis in ("close_to_close", "close_to_prev_settle"):
        out = chain_returns(_roll_prices(), ROLL, basis=basis)
        row = out.iloc[0]
        assert row["product_return"] == pytest.approx(0.05), basis
        assert row["value_share_new"] == pytest.approx(0.25), basis


def test_without_blending_a_roll_earns_the_leg_held_into_the_day():
    out = chain_returns(_roll_prices(), ROLL, roll_blend=False)
    assert out["product_return"].iloc[0] == pytest.approx(0.04)


def test_the_value_share_uses_the_settlement_when_that_is_the_basis():
    # Same legs but yesterday's settlements are 200 and 200, so the share is 0.5
    # rather than 0.25 -- the weighting follows whatever the return is measured
    # against, or the blend would weigh one thing and measure another.
    prices = _prices(
        [
            ("2010-01-04", "M2001.DCE", 300.0, 200.0),
            ("2010-01-04", "M2005.DCE", 100.0, 200.0),
            ("2010-01-05", "M2001.DCE", 312.0, 312.0),
            ("2010-01-05", "M2005.DCE", 108.0, 108.0),
        ]
    )
    out = chain_returns(prices, ROLL, basis="close_to_prev_settle")
    assert out["value_share_new"].iloc[0] == pytest.approx(0.5)


def test_a_roll_into_a_contract_with_no_prior_bar_falls_back_to_the_old_leg():
    # The new contract listed today, so it has no base to measure against.
    # Earning the old leg is the only honest answer; synthesising one is not.
    prices = _roll_prices()
    prices = prices.loc[
        ~(
            (prices["trade_date"] == date(2010, 1, 4))
            & (prices["contract"] == "M2005.DCE")
        )
    ]
    out = chain_returns(prices, ROLL, basis="close_to_close")
    row = out.iloc[0]
    assert row["product_return"] == pytest.approx(0.04)
    assert pd.isna(row["value_share_new"])


def test_an_ordinary_day_reports_no_roll_share():
    out = chain_returns(FLAT, HELD, basis="close_to_close")
    row = out.iloc[0]
    assert bool(row["is_roll"]) is False
    # The column is a roll diagnostic; a 0.5 on every ordinary day makes it useless.
    assert pd.isna(row["value_share_new"])
