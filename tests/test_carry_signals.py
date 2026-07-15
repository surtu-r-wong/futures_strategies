from dataclasses import FrozenInstanceError
from datetime import date

import pandas as pd
import pytest

from cta_carry.config import CarryConfig
from cta_carry.signals import SignalResult, build_signals


SIGNAL_COLUMNS = [
    "trade_date",
    "product",
    "main_contract",
    "main_close",
    "main_volume",
    "main_oi",
    "carry_ma",
    "atr",
    "price_ma",
    "volume_ma",
    "oi_ma",
    "input_ready",
    "rank_direction",
    "strength",
    "effective_direction",
    "reason",
]


def _config(**overrides):
    values = {
        "momentum_window": 2,
        "selection_fraction": 0.20,
    }
    values.update(overrides)
    return CarryConfig(**values)


def _row(
    trade_date,
    product,
    carry_ma,
    *,
    main_contract=None,
    main_close=100.0,
    main_volume=100.0,
    main_oi=100.0,
    atr=2.0,
):
    return {
        "trade_date": trade_date,
        "product": product,
        "main_contract": main_contract or f"{product}2405",
        "main_close": main_close,
        "main_volume": main_volume,
        "main_oi": main_oi,
        "carry_ma": carry_ma,
        "atr": atr,
    }


def _two_day_cross_section(carries, latest=None):
    dates = pd.bdate_range("2024-01-02", periods=2).date.tolist()
    latest = latest or {}
    rows = []
    for day_index, trade_date in enumerate(dates):
        for product, carry_ma in carries.items():
            overrides = latest.get(product, {}) if day_index == 1 else {}
            rows.append(
                _row(
                    trade_date,
                    product,
                    carry_ma,
                    **overrides,
                )
            )
    return pd.DataFrame(rows), dates


def _latest_by_product(result):
    latest_date = result.signals["trade_date"].max()
    return result.signals[
        result.signals["trade_date"] == latest_date
    ].set_index("product")


def test_five_product_ranks_and_trend_filters_start_on_second_day() -> None:
    frame, dates = _two_day_cross_section(
        {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5},
        latest={
            "A": {"main_close": 110.0},
            "E": {"main_close": 90.0},
        },
    )

    result = build_signals(frame, _config())
    latest = _latest_by_product(result)

    assert result.signal_ready_date == dates[1]
    assert result.signals[["trade_date", "product"]].values.tolist() == [
        [trade_date, product]
        for trade_date in dates
        for product in ["A", "B", "C", "D", "E"]
    ]
    assert latest["rank_direction"].to_dict() == {
        "A": 1,
        "B": 0,
        "C": 0,
        "D": 0,
        "E": -1,
    }
    assert latest.loc["A", "strength"] == 1.0
    assert latest.loc["E", "strength"] == 1.0
    assert latest.loc["A", "effective_direction"] == 1
    assert latest.loc["E", "effective_direction"] == -1
    assert set(latest["reason"]) == {"rank_and_filter"}
    first_day = result.signals[result.signals["trade_date"] == dates[0]]
    assert first_day["input_ready"].tolist() == [False] * 5
    assert set(first_day["reason"]) == {"insufficient_cross_section"}


def test_reverse_trend_uses_half_strength_only_when_volume_and_oi_are_low() -> None:
    carries = {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5}
    half_frame, _ = _two_day_cross_section(
        carries,
        latest={
            "A": {
                "main_close": 90.0,
                "main_volume": 80.0,
                "main_oi": 80.0,
            }
        },
    )
    zero_frame, _ = _two_day_cross_section(
        carries,
        latest={
            "A": {
                "main_close": 90.0,
                "main_volume": 80.0,
                "main_oi": 120.0,
            }
        },
    )

    half = _latest_by_product(build_signals(half_frame, _config())).loc["A"]
    zero = _latest_by_product(build_signals(zero_frame, _config())).loc["A"]

    assert half["rank_direction"] == 1
    assert half["strength"] == 0.5
    assert half["effective_direction"] == 1
    assert zero["rank_direction"] == 1
    assert zero["strength"] == 0.0
    assert zero["effective_direction"] == 0


def test_equal_price_volume_and_oi_moving_averages_give_zero_strength() -> None:
    frame, _ = _two_day_cross_section(
        {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5}
    )

    row = _latest_by_product(build_signals(frame, _config())).loc["A"]

    assert row["main_close"] == row["price_ma"]
    assert row["main_volume"] == row["volume_ma"]
    assert row["main_oi"] == row["oi_ma"]
    assert row["rank_direction"] == 1
    assert row["strength"] == 0.0
    assert row["effective_direction"] == 0


def test_four_ready_products_never_start_cross_sectional_signals() -> None:
    frame, _ = _two_day_cross_section(
        {"A": -0.5, "B": -0.2, "C": 0.2, "D": 0.5}
    )

    result = build_signals(frame, _config())
    latest = _latest_by_product(result)

    assert result.signal_ready_date is None
    assert latest["input_ready"].tolist() == [True] * 4
    assert latest["rank_direction"].tolist() == [0] * 4
    assert latest["strength"].tolist() == [0.0] * 4
    assert latest["effective_direction"].tolist() == [0] * 4
    assert set(latest["reason"]) == {"insufficient_cross_section"}


def test_main_contract_roll_uses_natural_product_price_history() -> None:
    dates = pd.bdate_range("2024-01-02", periods=3).date.tolist()
    frame = pd.DataFrame(
        [
            _row(
                dates[0],
                "A",
                -0.5,
                main_contract="A2405",
                main_close=100.0,
            ),
            _row(
                dates[1],
                "A",
                -0.5,
                main_contract="A2409",
                main_close=200.0,
            ),
            _row(
                dates[2],
                "A",
                -0.5,
                main_contract="A2409",
                main_close=202.0,
            ),
        ]
    )

    result = build_signals(frame, _config())

    assert result.signals["main_contract"].tolist() == [
        "A2405",
        "A2409",
        "A2409",
    ]
    assert pd.isna(result.signals.loc[0, "price_ma"])
    assert result.signals["price_ma"].tolist()[1:] == [150.0, 201.0]


def test_carry_ties_break_by_product_and_sign_gate_blocks_wrong_sign() -> None:
    tied_frame, _ = _two_day_cross_section(
        {"E": 0.5, "D": 0.5, "C": 0.0, "B": -0.2, "A": -0.2},
        latest={
            "A": {"main_close": 110.0},
            "E": {"main_close": 90.0},
        },
    )
    positive_frame, _ = _two_day_cross_section(
        {"A": 0.1, "B": 0.2, "C": 0.3, "D": 0.4, "E": 0.5},
        latest={"E": {"main_close": 90.0}},
    )

    tied = _latest_by_product(build_signals(tied_frame, _config()))
    positive = _latest_by_product(build_signals(positive_frame, _config()))

    assert tied["rank_direction"].to_dict() == {
        "A": 1,
        "B": 0,
        "C": 0,
        "D": 0,
        "E": -1,
    }
    assert positive.loc["A", "rank_direction"] == 0
    assert positive.loc["E", "rank_direction"] == -1


def test_missing_atr_does_not_count_toward_ready_cross_section() -> None:
    frame, _ = _two_day_cross_section(
        {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5},
        latest={"E": {"atr": float("nan")}},
    )

    result = build_signals(frame, _config())
    latest = _latest_by_product(result)

    assert latest["input_ready"].sum() == 4
    assert result.signal_ready_date is None
    assert latest["rank_direction"].tolist() == [0] * 5
    assert latest["effective_direction"].tolist() == [0] * 5
    assert set(latest["reason"]) == {"insufficient_cross_section"}


def test_empty_signal_result_is_frozen_and_has_stable_columns() -> None:
    result = build_signals(pd.DataFrame(), _config())

    assert isinstance(result, SignalResult)
    assert result.signals.empty
    assert result.signals.columns.tolist() == SIGNAL_COLUMNS
    assert result.signal_ready_date is None
    with pytest.raises(FrozenInstanceError):
        result.signal_ready_date = date(2024, 1, 2)
