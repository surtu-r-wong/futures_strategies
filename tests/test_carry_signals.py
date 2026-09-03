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
    "trend_state",
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
    return result.signals[result.signals["trade_date"] == latest_date].set_index(
        "product"
    )


def _price_path_cross_section(closes, carries):
    """Cross-section where product A walks `closes` and the rest stay flat."""
    dates = pd.bdate_range("2024-01-02", periods=len(closes)).date.tolist()
    rows = []
    for day_index, trade_date in enumerate(dates):
        for product, carry_ma in carries.items():
            rows.append(
                _row(
                    trade_date,
                    product,
                    carry_ma,
                    main_close=closes[day_index] if product == "A" else 100.0,
                )
            )
    return pd.DataFrame(rows), dates


def test_signals_expose_trend_state_for_audit() -> None:
    """The trend state drives strength, so it has to be auditable in the sheet."""
    carries = {"A": 0.5, "B": 0.2, "C": 0.0, "D": -0.2, "E": -0.5}
    frame, _ = _price_path_cross_section([100.0, 106.0, 112.0], carries)

    latest = _latest_by_product(build_signals(frame, _config(trend_band_atr=1.0)))

    assert latest.loc["A", "trend_state"] == 1
    # E never left the band around its own flat MA.
    assert latest.loc["E", "trend_state"] == 0


def test_missing_atr_freezes_the_trend_state_instead_of_flipping_it() -> None:
    """A day with no ATR cannot say where the band is, so the trend state holds
    rather than guessing.  With the ATR present that same day flips it."""
    carries = {"A": 0.5, "B": 0.2, "C": 0.0, "D": -0.2, "E": -0.5}
    closes = [100.0, 106.0, 80.0, 84.0]

    def _frame(third_day_atr):
        dates = pd.bdate_range("2024-01-02", periods=4).date.tolist()
        atrs = [2.0, 2.0, third_day_atr, 2.0]
        rows = []
        for day_index, trade_date in enumerate(dates):
            for product, carry_ma in carries.items():
                if product == "A":
                    rows.append(
                        _row(
                            trade_date,
                            product,
                            carry_ma,
                            main_close=closes[day_index],
                            atr=atrs[day_index],
                        )
                    )
                else:
                    rows.append(_row(trade_date, product, carry_ma))
        return pd.DataFrame(rows)

    config = _config(trend_band_atr=1.0)
    # Day 2 sets the state to +1 (close 106 clears MA 103 by a full ATR).  Day 3
    # would push it to -1 (close 80 vs MA 93), and day 4 lands inside the band.
    frozen = _latest_by_product(build_signals(_frame(float("nan")), config)).loc["A"]
    flipped = _latest_by_product(build_signals(_frame(2.0), config)).loc["A"]

    assert frozen["rank_direction"] == 1
    assert frozen["strength"] == 1.0
    assert flipped["rank_direction"] == 1
    assert flipped["strength"] == 0.0


def test_trend_state_is_tracked_per_product_without_bleeding_across() -> None:
    """One product's breakout must not move another product's trend state."""
    carries = {"A": 0.5, "B": 0.2, "C": 0.0, "D": -0.2, "E": -0.5}
    dates = pd.bdate_range("2024-01-02", periods=3).date.tolist()
    # A rallies clear of its band; E is the short leg and stays perfectly flat.
    a_closes = [100.0, 106.0, 112.0]
    rows = []
    for day_index, trade_date in enumerate(dates):
        for product, carry_ma in carries.items():
            rows.append(
                _row(
                    trade_date,
                    product,
                    carry_ma,
                    main_close=a_closes[day_index] if product == "A" else 100.0,
                )
            )

    latest = _latest_by_product(
        build_signals(pd.DataFrame(rows), _config(trend_band_atr=1.0))
    )

    assert latest.loc["A", "strength"] == 1.0
    # E never moved, so its state is still flat and it takes no position.
    assert latest.loc["E", "rank_direction"] == -1
    assert latest.loc["E", "strength"] == 0.0


def test_confirm_days_requires_consecutive_same_side_closes_before_flipping() -> None:
    """With trend_confirm_days=3 the trend state only flips after three straight
    closes on the same side of the MA; two are not enough.  The default of 1
    flips on the first."""
    carries = {"A": 0.5, "B": 0.2, "C": 0.0, "D": -0.2, "E": -0.5}
    # momentum_window=2.  Day 1's MA is NaN, so days 2..n are the same-side run:
    # day2 close 102 vs MA 101, day3 104 vs 103, day4 106 vs 105.
    two_day_run, _ = _price_path_cross_section([100.0, 102.0, 104.0], carries)
    three_day_run, _ = _price_path_cross_section([100.0, 102.0, 104.0, 106.0], carries)

    slow_after_two = _latest_by_product(
        build_signals(two_day_run, _config(trend_confirm_days=3))
    ).loc["A"]
    slow_after_three = _latest_by_product(
        build_signals(three_day_run, _config(trend_confirm_days=3))
    ).loc["A"]
    fast = _latest_by_product(build_signals(two_day_run, _config())).loc["A"]

    assert slow_after_two["rank_direction"] == 1
    assert slow_after_two["strength"] == 0.0

    assert slow_after_three["rank_direction"] == 1
    assert slow_after_three["strength"] == 1.0

    assert fast["rank_direction"] == 1
    assert fast["strength"] == 1.0


def test_atr_band_holds_trend_state_when_price_pops_back_inside_band() -> None:
    """A short leg that dips and then pops back just above its MA keeps full
    strength while the pop stays inside the ATR band.  A bare MA cross (the
    default, band=0) flips the trend state and zeroes the position instead --
    that flip is the whipsaw the band exists to absorb."""
    carries = {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5}
    # momentum_window=2, atr=2.0.  Day 3 puts A clearly below its MA (90 vs 95);
    # day 4's MA is 92, so close=94 sits exactly on the +1 ATR band edge.
    frame, _ = _price_path_cross_section([100.0, 100.0, 90.0, 94.0], carries)

    banded = _latest_by_product(build_signals(frame, _config(trend_band_atr=1.0))).loc[
        "A"
    ]
    bare = _latest_by_product(build_signals(frame, _config())).loc["A"]

    assert banded["rank_direction"] == -1
    assert banded["strength"] == 1.0
    assert banded["effective_direction"] == -1

    assert bare["rank_direction"] == -1
    assert bare["strength"] == 0.0
    assert bare["effective_direction"] == 0


def test_momentum_ma_rolls_over_curve_rows_and_spans_missing_days() -> None:
    """Documented deviation (design spec 7, '选中的主力观测序列'): momentum MAs roll
    over the days a product actually has a curve row -- pooled AND with a
    strictly-later secondary.  A pooled day with a main but no secondary yields
    no curve row, so a later window spans that gap rather than resetting on it or
    reading a value for the missing day.  Locks the chosen (kept) behavior.
    """
    all_dates = pd.bdate_range("2024-01-02", periods=5).date.tolist()
    present = [all_dates[0], all_dates[1], all_dates[2], all_dates[4]]  # index 3 absent
    closes = [100.0, 110.0, 120.0, 130.0]
    frame = pd.DataFrame(
        [
            _row(trade_date, "RB", -0.5, main_close=close)
            for trade_date, close in zip(present, closes)
        ]
    )

    result = build_signals(frame, _config(momentum_window=3))
    price_ma = result.signals.set_index("trade_date")["price_ma"]

    assert pd.isna(price_ma[all_dates[0]])
    assert pd.isna(price_ma[all_dates[1]])
    assert price_ma[all_dates[2]] == pytest.approx((100.0 + 110.0 + 120.0) / 3)
    # The window on the 4th present row spans the absent day -- mean(110,120,130) --
    # it is not reset by the gap and reads no value for the missing day.
    assert price_ma[all_dates[4]] == pytest.approx((110.0 + 120.0 + 130.0) / 3)


def test_five_product_ranks_and_trend_filters_start_on_second_day() -> None:
    frame, dates = _two_day_cross_section(
        {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5},
        latest={
            "A": {"main_close": 90.0},
            "E": {"main_close": 110.0},
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
        "A": -1,
        "B": 0,
        "C": 0,
        "D": 0,
        "E": 1,
    }
    assert latest.loc["A", "strength"] == 1.0
    assert latest.loc["E", "strength"] == 1.0
    assert latest.loc["A", "effective_direction"] == -1
    assert latest.loc["E", "effective_direction"] == 1
    assert set(latest["reason"]) == {"rank_and_filter"}
    first_day = result.signals[result.signals["trade_date"] == dates[0]]
    assert first_day["input_ready"].tolist() == [False] * 5
    assert set(first_day["reason"]) == {"insufficient_cross_section"}


def test_backwardation_is_long_and_contango_is_short() -> None:
    """Carry direction. ``carry_raw = main_close / secondary_close - 1``, so a
    positive ``carry_ma`` means the dominant (near) contract trades above the
    later one -- backwardation -- and a negative one means contango. The carry
    premium is earned by holding the backwardated side, so the most positive
    carry is the long leg and the most negative carry is the short leg.
    """
    frame, _ = _two_day_cross_section(
        {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5},
        latest={
            "A": {"main_close": 90.0},
            "E": {"main_close": 110.0},
        },
    )

    latest = _latest_by_product(build_signals(frame, _config()))

    assert latest["rank_direction"].to_dict() == {
        "A": -1,
        "B": 0,
        "C": 0,
        "D": 0,
        "E": 1,
    }
    assert latest.loc["A", "strength"] == 1.0
    assert latest.loc["E", "strength"] == 1.0
    assert latest.loc["A", "effective_direction"] == -1
    assert latest.loc["E", "effective_direction"] == 1


def test_reverse_trend_uses_half_strength_only_when_volume_and_oi_are_low() -> None:
    carries = {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5}
    # A is the most contango product, so it is the short leg; a rising price is
    # therefore the reverse trend that the contraction filter has to judge.
    half_frame, _ = _two_day_cross_section(
        carries,
        latest={
            "A": {
                "main_close": 110.0,
                "main_volume": 80.0,
                "main_oi": 80.0,
            }
        },
    )
    zero_frame, _ = _two_day_cross_section(
        carries,
        latest={
            "A": {
                "main_close": 110.0,
                "main_volume": 80.0,
                "main_oi": 120.0,
            }
        },
    )

    half = _latest_by_product(build_signals(half_frame, _config())).loc["A"]
    zero = _latest_by_product(build_signals(zero_frame, _config())).loc["A"]

    assert half["rank_direction"] == -1
    assert half["strength"] == 0.5
    assert half["effective_direction"] == -1
    assert zero["rank_direction"] == -1
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
    assert row["rank_direction"] == -1
    assert row["strength"] == 0.0
    assert row["effective_direction"] == 0


def test_four_ready_products_never_start_cross_sectional_signals() -> None:
    frame, _ = _two_day_cross_section({"A": -0.5, "B": -0.2, "C": 0.2, "D": 0.5})

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
        "A": -1,
        "B": 0,
        "C": 0,
        "D": 0,
        "E": 1,
    }
    # All carries positive: the bottom of the ranking is still contango-gated,
    # so the short leg stays empty while the top takes the long.
    assert positive.loc["A", "rank_direction"] == 0
    assert positive.loc["E", "rank_direction"] == 1


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


def test_equal_price_ma_blocks_half_strength_despite_double_contraction() -> None:
    frame, _ = _two_day_cross_section(
        {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5},
        latest={
            "A": {
                "main_close": 100.0,
                "main_volume": 80.0,
                "main_oi": 80.0,
            }
        },
    )

    row = _latest_by_product(build_signals(frame, _config())).loc["A"]

    assert row["rank_direction"] == -1
    assert row["main_close"] == row["price_ma"]
    assert row["main_volume"] < row["volume_ma"]
    assert row["main_oi"] < row["oi_ma"]
    assert row["strength"] == 0.0
    assert row["effective_direction"] == 0


def _opposed_and_fading(carries, closes):
    """A rises against a short carry rank while its volume and OI both fade,
    which is exactly the condition the 0.5 branch exists for."""
    dates = pd.bdate_range("2024-01-02", periods=len(closes)).date.tolist()
    rows = []
    for day, trade_date in enumerate(dates):
        for product, carry_ma in carries.items():
            fading = product == "A"
            rows.append(
                _row(
                    trade_date,
                    product,
                    carry_ma,
                    main_close=closes[day] if fading else 100.0,
                    main_volume=(100.0 - 12.0 * day) if fading else 100.0,
                    main_oi=(100.0 - 12.0 * day) if fading else 100.0,
                )
            )
    return pd.DataFrame(rows)


def test_the_opposed_but_fading_branch_can_be_switched_off():
    """The 0.5 branch trades against the trend when volume and open interest
    are both fading. Attribution over 2013-2026 puts its contribution at -0.102
    across the three loss-making windows against +0.220 for the trend-aligned
    branch, so it has to be testable without it. The default stays on: the
    baseline must not move."""
    carries = {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5}
    frame = _opposed_and_fading(carries, [100.0, 100.0, 104.0, 108.0])

    on = _latest_by_product(build_signals(frame, _config())).loc["A"]
    off = _latest_by_product(
        build_signals(frame, _config(allow_trend_opposed=False))
    ).loc["A"]

    assert on["strength"] == 0.5
    assert on["effective_direction"] == -1
    assert off["strength"] == 0.0
    assert off["effective_direction"] == 0


def test_switching_the_branch_off_leaves_trend_aligned_positions_alone():
    """Reverse guard: the switch removes one branch, not the strategy."""
    carries = {"A": 0.5, "B": 0.2, "C": 0.0, "D": -0.2, "E": -0.5}
    frame, _ = _price_path_cross_section([100.0, 100.0, 104.0, 108.0], carries)

    on = _latest_by_product(build_signals(frame, _config())).loc["A"]
    off = _latest_by_product(
        build_signals(frame, _config(allow_trend_opposed=False))
    ).loc["A"]

    assert on["strength"] == 1.0
    assert off["strength"] == 1.0
    assert off["effective_direction"] == on["effective_direction"]


def test_the_whole_trend_filter_can_be_switched_off():
    """The momentum/volume filter zeroes 31.4% of the ranked product-days and,
    because it is a hysteresis-free switch, 53.3% of all turnover is round
    trips it causes (2026-08-06 attribution). Turnover is the binding
    constraint on this strategy, so the filter has to be measurable without
    it. With the filter off the Carry ranking alone decides the position.
    The default stays on: the baseline must not move."""
    carries = {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5}
    frame, _ = _two_day_cross_section(carries)

    on = _latest_by_product(build_signals(frame, _config())).loc["A"]
    off = _latest_by_product(
        build_signals(frame, _config(trend_filter_enabled=False))
    ).loc["A"]

    assert on["rank_direction"] == -1
    assert on["strength"] == 0.0
    assert on["effective_direction"] == 0

    assert off["rank_direction"] == -1
    assert off["strength"] == 1.0
    assert off["effective_direction"] == -1


def test_switching_the_trend_filter_off_still_leaves_unranked_products_flat():
    """Reverse guard: the switch removes the filter, not the ranking gate.
    A product the cross-section never selects must stay flat either way."""
    carries = {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5}
    frame, _ = _two_day_cross_section(carries)

    off = _latest_by_product(
        build_signals(frame, _config(trend_filter_enabled=False))
    ).loc["C"]

    assert off["rank_direction"] == 0
    assert off["strength"] == 0.0
    assert off["effective_direction"] == 0
