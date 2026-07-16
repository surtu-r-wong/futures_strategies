from dataclasses import FrozenInstanceError
from datetime import date

import pandas as pd
import pytest

from cta_carry.backtest import (
    CarryBacktester,
    CarryBacktestResult,
    ClosePlan,
    ExecutionPriceError,
    EquityDepletedError,
    SignalInputError,
    WarmupInsufficientError,
    _close_plan,
    _validate_target_opens,
    contract_gross_return,
    initial_report_row,
    ordinary_ledger_row,
    weight_turnover,
)
from cta_carry.data import CarryDataSet
from cta_carry.risk import PositionState

from tests.carry_fixtures import make_carry_panel, small_config


TRADE_DATE = date(2024, 1, 3)


def test_contract_gross_return_and_weight_turnover_use_concrete_contracts() -> None:
    gross_return = contract_gross_return(
        {"A": 0.5, "B": -0.25},
        {"A": 100.0, "B": 200.0},
        {"A": 110.0, "B": 180.0},
        trade_date=TRADE_DATE,
    )
    turnover = weight_turnover(
        {"A": 0.5},
        {"A": 0.2, "B": -0.3},
    )

    assert gross_return == pytest.approx(0.075)
    assert turnover == pytest.approx(0.6)


def test_missing_current_open_raises_structured_execution_price_error() -> None:
    with pytest.raises(ExecutionPriceError) as exc_info:
        contract_gross_return(
            {"A": 0.5},
            {"A": 100.0},
            {},
            trade_date=TRADE_DATE,
        )

    error = exc_info.value
    assert isinstance(error, RuntimeError)
    assert error.trade_date == TRADE_DATE
    assert error.contract == "A"
    assert error.check == "open_price"
    assert error.reason == "current open is missing"
    assert str(error) == ("2024-01-03 A open_price: current open is missing")


def test_execution_error_includes_engine_product_and_context() -> None:
    with pytest.raises(ExecutionPriceError) as exc_info:
        contract_gross_return(
            {"A": 0.5},
            {"A": 100.0},
            {},
            trade_date=TRADE_DATE,
            contract_products={"A": "PRODUCT_A"},
            context="formal_portfolio",
        )

    error = exc_info.value
    assert error.product == "PRODUCT_A"
    assert error.context == "formal_portfolio"
    assert error.value is None
    assert "product=PRODUCT_A" in str(error)
    assert "context=formal_portfolio" in str(error)


@pytest.mark.parametrize(
    ("previous_open", "reason"),
    [
        ({}, "previous open is missing"),
        ({"A": float("nan")}, "previous open must be finite and positive"),
        ({"A": 0.0}, "previous open must be finite and positive"),
        ({"A": -1.0}, "previous open must be finite and positive"),
    ],
)
def test_invalid_previous_open_raises_execution_price_error(
    previous_open, reason
) -> None:
    with pytest.raises(ExecutionPriceError) as exc_info:
        contract_gross_return(
            {"A": 0.5},
            previous_open,
            {"A": 110.0},
            trade_date=TRADE_DATE,
        )

    assert exc_info.value.reason == reason
    assert "2024-01-03 A open_price" in str(exc_info.value)


@pytest.mark.parametrize("current_open", [{"A": float("inf")}, {"A": 0.0}])
def test_invalid_current_open_raises_execution_price_error(current_open) -> None:
    with pytest.raises(ExecutionPriceError) as exc_info:
        contract_gross_return(
            {"A": 0.5},
            {"A": 100.0},
            current_open,
            trade_date=TRADE_DATE,
        )

    assert exc_info.value.reason == ("current open must be finite and positive")


def test_zero_weight_contract_does_not_require_open_prices() -> None:
    assert (
        contract_gross_return(
            {"A": 0.0},
            {},
            {},
            trade_date=TRADE_DATE,
        )
        == 0.0
    )


def test_ordinary_ledger_row_applies_cost_and_equity_identity() -> None:
    row = ordinary_ledger_row(
        trade_date=TRADE_DATE,
        previous_equity=2.0,
        gross_return=0.05,
        turnover=0.6,
        cost_bps=10.0,
    )

    assert row == {
        "trade_date": TRADE_DATE,
        "gross_return": 0.05,
        "turnover": 0.6,
        "cost": pytest.approx(0.0006),
        "net_return": pytest.approx(0.0494),
        "equity": pytest.approx(2.0988),
        "boundary_type": "ordinary",
    }
    assert row["equity"] == pytest.approx(2.0 * (1.0 + row["net_return"]))


def test_initial_report_row_resets_equity_before_open_rebalance() -> None:
    row = initial_report_row(
        trade_date=TRADE_DATE,
        carried_weights={"A": 0.5},
        target_weights={"A": 0.2, "B": -0.3},
        cost_bps=10.0,
    )

    assert row["gross_return"] == 0.0
    assert row["turnover"] == pytest.approx(0.6)
    assert row["cost"] == pytest.approx(0.0006)
    assert row["net_return"] == pytest.approx(-0.0006)
    assert row["equity"] == pytest.approx(0.9994)
    assert row["boundary_type"] == "report_start_initialization"


@pytest.mark.parametrize("gross_return", [-1.0, -1.25])
def test_ordinary_ledger_stops_at_nonpositive_equity(gross_return) -> None:
    with pytest.raises(EquityDepletedError) as exc_info:
        ordinary_ledger_row(
            trade_date=TRADE_DATE,
            previous_equity=1.0,
            gross_return=gross_return,
            turnover=0.0,
            cost_bps=0.0,
        )

    error = exc_info.value
    assert error.trade_date == TRADE_DATE
    assert error.previous_equity == 1.0
    assert error.gross_return == gross_return
    assert error.net_return == gross_return
    assert error.equity <= 0.0
    assert "equity depleted" in str(error)


def test_initial_report_stops_if_initial_cost_depletes_equity() -> None:
    with pytest.raises(EquityDepletedError) as exc_info:
        initial_report_row(
            trade_date=TRADE_DATE,
            carried_weights={"A": 1.0},
            target_weights={},
            cost_bps=10_000.0,
        )

    error = exc_info.value
    assert error.previous_equity == 1.0
    assert error.gross_return == 0.0
    assert error.turnover == 1.0
    assert error.cost == 1.0
    assert error.net_return == -1.0
    assert error.equity == 0.0


def test_ordinary_ledger_rejects_depleted_previous_equity() -> None:
    with pytest.raises(ValueError, match="previous_equity"):
        ordinary_ledger_row(
            trade_date=TRADE_DATE,
            previous_equity=0.0,
            gross_return=0.0,
            turnover=0.0,
            cost_bps=0.0,
        )


def test_warmup_error_reports_shadow_and_active_gaps() -> None:
    error = WarmupInsufficientError(
        query_start=date(2023, 1, 1),
        report_start_date=date(2024, 1, 2),
        signal_ready_date=date(2023, 12, 1),
        shadow_observations=118,
        active_days=120,
        required_observations=120,
        required_active_days=126,
    )

    assert isinstance(error, RuntimeError)
    assert error.query_start == date(2023, 1, 1)
    assert error.report_start_date == date(2024, 1, 2)
    assert error.signal_ready_date == date(2023, 12, 1)
    assert error.shadow_observations == 118
    assert error.active_days == 120
    assert error.required_observations == 120
    assert error.required_active_days == 126
    assert error.shadow_gap == 2
    assert error.active_gap == 6
    message = str(error)
    assert "risk scaling not ready" in message
    assert "query_start=2023-01-01" in message
    assert "report_start_date=2024-01-02" in message
    assert "signal_ready_date=2023-12-01" in message
    assert "shadow=118/120" in message
    assert "active=120/126" in message


@pytest.mark.parametrize(
    ("weights", "message"),
    [
        ({"A": float("nan")}, "weight"),
        ({"A": float("inf")}, "weight"),
    ],
)
def test_contract_gross_return_rejects_nonfinite_weights(weights, message) -> None:
    with pytest.raises(ValueError, match=message):
        contract_gross_return(
            weights,
            {"A": 100.0},
            {"A": 110.0},
            trade_date=TRADE_DATE,
        )


def test_contract_gross_return_rejects_nonfinite_total() -> None:
    with pytest.raises(ValueError, match="gross return"):
        contract_gross_return(
            {"A": 1.0},
            {"A": 1e-308},
            {"A": 1e308},
            trade_date=TRADE_DATE,
        )


@pytest.mark.parametrize(
    ("old_weights", "new_weights"),
    [
        ({"A": float("nan")}, {}),
        ({}, {"A": float("inf")}),
    ],
)
def test_weight_turnover_rejects_nonfinite_weights(old_weights, new_weights) -> None:
    with pytest.raises(ValueError, match="weight"):
        weight_turnover(old_weights, new_weights)


@pytest.mark.parametrize(
    "overrides",
    [
        {"previous_equity": float("nan")},
        {"gross_return": float("inf")},
        {"turnover": float("nan")},
        {"cost_bps": float("nan")},
    ],
)
def test_ordinary_ledger_row_rejects_nonfinite_inputs(overrides) -> None:
    values = {
        "trade_date": TRADE_DATE,
        "previous_equity": 1.0,
        "gross_return": 0.01,
        "turnover": 0.1,
        "cost_bps": 10.0,
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        ordinary_ledger_row(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("turnover", -0.1),
        ("cost_bps", -1.0),
    ],
)
def test_ordinary_ledger_row_rejects_negative_inputs(field, value) -> None:
    values = {
        "trade_date": TRADE_DATE,
        "previous_equity": 1.0,
        "gross_return": 0.01,
        "turnover": 0.1,
        "cost_bps": 10.0,
    }
    values[field] = value

    with pytest.raises(ValueError, match=field):
        ordinary_ledger_row(**values)


def test_ordinary_ledger_row_rejects_nonfinite_output() -> None:
    with pytest.raises(ValueError, match="ledger"):
        ordinary_ledger_row(
            trade_date=TRADE_DATE,
            previous_equity=1e308,
            gross_return=1.0,
            turnover=0.0,
            cost_bps=0.0,
        )


@pytest.mark.parametrize("cost_bps", [-1.0, float("nan")])
def test_initial_report_row_rejects_invalid_cost_bps(cost_bps) -> None:
    with pytest.raises(ValueError, match="cost_bps"):
        initial_report_row(
            trade_date=TRADE_DATE,
            carried_weights={},
            target_weights={},
            cost_bps=cost_bps,
        )


def _empty_backtest_result() -> CarryBacktestResult:
    return CarryBacktestResult(
        daily_returns=pd.DataFrame(),
        positions=pd.DataFrame(),
        trades=pd.DataFrame(),
        signals=pd.DataFrame(),
        curve_selection=pd.DataFrame(),
        data_quality=pd.DataFrame(),
        run_config=pd.DataFrame(),
    )


def test_backtest_result_is_frozen_and_metrics_default_is_not_shared() -> None:
    first = _empty_backtest_result()
    second = _empty_backtest_result()

    assert first.metrics == {}
    assert first.metrics is not second.metrics
    first.metrics["sharpe"] = 1.0
    assert second.metrics == {}
    with pytest.raises(FrozenInstanceError):
        first.run_config = {"changed": True}


DAILY_COLUMNS = [
    "trade_date",
    "gross_return",
    "turnover",
    "cost",
    "net_return",
    "equity",
    "gross_leverage",
    "boundary_type",
]
POSITION_COLUMNS = [
    "trade_date",
    "product",
    "contract",
    "direction",
    "raw_weight",
    "weight",
    "gross_leverage",
    "tranches_remaining",
    "highest_high",
    "lowest_low",
    "locked_direction",
    "carried_in",
]
TRADE_COLUMNS = [
    "trade_date",
    "product",
    "contract",
    "old_weight",
    "new_weight",
    "weight_change",
    "reason",
]


def _run_stateful(*, periods=24, start_index=12, end_index=-1):
    data = make_carry_panel(periods=periods)
    dates = data.dates
    result = CarryBacktester(
        data,
        small_config(),
        start=dates[start_index],
        end=dates[end_index],
    ).run()
    return data, result


def _run_config_values(result):
    return dict(result.run_config[["key", "value"]].itertuples(index=False))


def test_engine_builds_execution_maps_one_day_at_a_time(monkeypatch) -> None:
    from cta_carry import backtest as backtest_module

    data = make_carry_panel()
    observed_dates = []
    original = backtest_module._bar_maps

    def checked_bar_maps(day_prices):
        dates = day_prices["trade_date"].drop_duplicates().tolist()
        assert len(dates) == 1
        observed_dates.append(dates[0])
        return original(day_prices)

    monkeypatch.setattr(backtest_module, "_bar_maps", checked_bar_maps)
    CarryBacktester(
        data,
        small_config(),
        start=data.dates[12],
        end=data.dates[-1],
    ).run()

    assert observed_dates == data.dates


def test_stateful_engine_starts_exactly_with_ready_carried_positions() -> None:
    data, result = _run_stateful()
    first = result.daily_returns.iloc[0]
    run_config = _run_config_values(result)

    assert result.daily_returns.columns.tolist() == DAILY_COLUMNS
    assert result.positions.columns.tolist() == POSITION_COLUMNS
    assert result.trades.columns.tolist() == TRADE_COLUMNS
    assert result.daily_returns["trade_date"].iloc[0] == data.dates[12]
    assert first["boundary_type"] == "report_start_initialization"
    assert first["gross_return"] == 0.0
    assert first["equity"] == pytest.approx(1.0 - first["cost"])
    assert run_config["report_start_date"] == data.dates[12]
    assert run_config["signal_ready_date"] is not None
    assert run_config["vol_ready_date"] is not None
    assert run_config["vol_ready_date"] <= data.dates[12]
    assert result.positions["carried_in"].any()
    assert result.positions["gross_leverage"].max() <= (
        small_config().max_gross_leverage + 1e-12
    )


def test_daily_equity_identity_and_initial_cost_boundary_are_exact() -> None:
    _, result = _run_stateful()
    daily = result.daily_returns.reset_index(drop=True)

    assert daily.loc[0, "equity"] == pytest.approx(1.0 - daily.loc[0, "cost"])
    assert daily.loc[0, "gross_return"] == 0.0
    for index in range(1, len(daily)):
        assert daily.loc[index, "equity"] == pytest.approx(
            daily.loc[index - 1, "equity"] * (1.0 + daily.loc[index, "net_return"])
        )
        assert daily.loc[index, "net_return"] == pytest.approx(
            daily.loc[index, "gross_return"] - daily.loc[index, "cost"]
        )


def test_future_inputs_cannot_change_prior_structured_outputs() -> None:
    data = make_carry_panel(periods=24)
    dates = data.dates
    cutoff = dates[18]
    changed_prices = data.prices.copy()
    future = changed_prices["trade_date"] > cutoff
    changed_prices.loc[future, ["open", "high", "low", "close"]] *= 1.7
    changed_prices.loc[future, "volume"] *= 1.3
    changed_prices.loc[future, "oi"] *= 1.4
    changed_prices.loc[future, "turnover"] *= 1.5
    changed = CarryDataSet(changed_prices, data.data_quality.copy())

    baseline = CarryBacktester(
        data,
        small_config(),
        start=dates[12],
        end=dates[-1],
    ).run()
    perturbed = CarryBacktester(
        changed,
        small_config(),
        start=dates[12],
        end=dates[-1],
    ).run()

    for field_name in (
        "daily_returns",
        "positions",
        "trades",
        "signals",
        "curve_selection",
    ):
        first = getattr(baseline, field_name)
        second = getattr(perturbed, field_name)
        first = first.loc[first["trade_date"] <= cutoff].reset_index(drop=True)
        second = second.loc[second["trade_date"] <= cutoff].reset_index(drop=True)
        pd.testing.assert_frame_equal(first, second)


def test_stateful_engine_is_deterministic_across_all_result_tables() -> None:
    data = make_carry_panel()
    values = {
        "data": data,
        "config": small_config(),
        "start": data.dates[12],
        "end": data.dates[-1],
    }

    first = CarryBacktester(**values).run()
    second = CarryBacktester(**values).run()

    for field_name in (
        "daily_returns",
        "positions",
        "trades",
        "signals",
        "curve_selection",
        "data_quality",
        "run_config",
    ):
        pd.testing.assert_frame_equal(
            getattr(first, field_name),
            getattr(second, field_name),
        )


def test_requested_start_is_never_slid_when_shadow_warmup_is_short() -> None:
    data = make_carry_panel()

    with pytest.raises(WarmupInsufficientError) as exc_info:
        CarryBacktester(
            data,
            small_config(),
            start=data.dates[6],
            end=data.dates[-1],
        ).run()

    error = exc_info.value
    assert error.report_start_date == data.dates[6]
    assert error.query_start == data.dates[0]
    assert error.signal_ready_date is not None
    assert error.shadow_observations < error.required_observations
    assert error.shadow_gap == (error.required_observations - error.shadow_observations)


def test_formal_and_raw_positions_share_state_and_respect_gross_cap() -> None:
    _, result = _run_stateful()
    active = result.positions.loc[result.positions["direction"] != 0].copy()

    assert not active.empty
    assert active["contract"].notna().all()
    assert (active["raw_weight"] * active["direction"] > 0.0).all()
    assert (active["weight"] * active["direction"] > 0.0).all()
    assert (
        active["tranches_remaining"]
        .between(
            1,
            small_config().stop_tranches,
        )
        .all()
    )
    assert active.loc[active["direction"] == 1, "highest_high"].notna().any()
    assert active.loc[active["direction"] == -1, "lowest_low"].notna().any()
    assert active["gross_leverage"].max() <= (small_config().max_gross_leverage + 1e-12)


def test_main_contract_roll_trades_both_legs_with_roll_reason() -> None:
    data = make_carry_panel(periods=26)
    prices = data.prices.copy()
    roll_date = data.dates[17]
    next_open = data.dates[18]
    product_a = (prices["trade_date"] == roll_date) & (prices["product"] == "A")
    prices.loc[product_a, "turnover"] = 0.0
    prices.loc[
        product_a & (prices["contract"] == "A2410"),
        ["oi", "volume"],
    ] = [100.0, 100.0]
    prices.loc[
        product_a & (prices["contract"] == "A2501"),
        ["oi", "volume"],
    ] = [700.0, 500.0]
    changed = CarryDataSet(prices, data.data_quality.copy())

    result = CarryBacktester(
        changed,
        small_config(),
        start=data.dates[12],
        end=data.dates[-1],
    ).run()
    selected = result.curve_selection.loc[
        (result.curve_selection["trade_date"] == roll_date)
        & (result.curve_selection["product"] == "A")
        & result.curve_selection["selected"]
    ]
    roll_trades = result.trades.loc[
        (result.trades["trade_date"] == next_open) & (result.trades["product"] == "A")
    ]

    assert selected.loc[selected["role"] == "main", "contract"].tolist() == ["A2501"]
    assert set(roll_trades["contract"]) == {"A2410", "A2501"}
    assert set(roll_trades["reason"]) == {"roll"}


@pytest.mark.parametrize(
    ("tranches_remaining", "expected_reason", "expected_remaining"),
    [(3, "stop_1", 2), (2, "stop_2", 1), (1, "stop_3", 0)],
)
def test_close_plan_emits_exact_stop_stage_reasons(
    tranches_remaining,
    expected_reason,
    expected_remaining,
) -> None:
    config = small_config()
    state = PositionState(
        direction=1,
        contract="A2410",
        tranches_remaining=tranches_remaining,
        highest_high=110.0,
    )
    signal_rows = pd.DataFrame(
        [
            {
                "product": "A",
                "main_contract": "A2410",
                "main_close": 90.0,
                "atr": 1.0,
                "strength": 1.0,
                "effective_direction": 1,
            }
        ]
    )

    plan = _close_plan(
        states={"A": state},
        signal_rows=signal_rows,
        bars={"A2410": {"high": 100.0, "low": 89.0, "close": 90.0}},
        atrs={"A2410": 1.0},
        config=config,
    )

    assert isinstance(plan, ClosePlan)
    assert plan.reasons["A"] == expected_reason
    assert plan.states["A"].tranches_remaining == expected_remaining
    if expected_remaining == 0:
        assert plan.states["A"].locked_direction == 1


def test_entry_extreme_starts_on_first_close_after_execution() -> None:
    config = small_config()
    signal_rows = pd.DataFrame(
        [
            {
                "product": "A",
                "main_contract": "A2410",
                "main_close": 100.0,
                "atr": 1.0,
                "strength": 1.0,
                "effective_direction": 1,
            }
        ]
    )

    entry = _close_plan(
        states={},
        signal_rows=signal_rows,
        bars={"A2410": {"high": 150.0, "low": 99.0, "close": 100.0}},
        atrs={"A2410": 1.0},
        config=config,
    )

    assert entry.states["A"].highest_high is None

    first_held_close = _close_plan(
        states=entry.states,
        signal_rows=signal_rows,
        bars={"A2410": {"high": 101.0, "low": 99.0, "close": 100.0}},
        atrs={"A2410": 1.0},
        config=config,
    )

    assert first_held_close.states["A"].highest_high == 101.0
    assert first_held_close.states["A"].tranches_remaining == config.stop_tranches


def test_roll_resets_extreme_until_new_contract_is_held() -> None:
    config = small_config()
    before = PositionState(
        direction=1,
        contract="A2410",
        tranches_remaining=config.stop_tranches,
        highest_high=111.0,
    )
    signal_rows = pd.DataFrame(
        [
            {
                "product": "A",
                "main_contract": "A2501",
                "main_close": 200.0,
                "atr": 2.0,
                "strength": 1.0,
                "effective_direction": 1,
            }
        ]
    )

    roll = _close_plan(
        states={"A": before},
        signal_rows=signal_rows,
        bars={
            "A2410": {"high": 111.0, "low": 109.0, "close": 110.0},
            "A2501": {"high": 250.0, "low": 190.0, "close": 200.0},
        },
        atrs={"A2410": 1.0, "A2501": 2.0},
        config=config,
    )

    assert roll.reasons["A"] == "roll"
    assert roll.states["A"].contract == "A2501"
    assert roll.states["A"].highest_high is None

    first_held_close = _close_plan(
        states=roll.states,
        signal_rows=signal_rows,
        bars={"A2501": {"high": 205.0, "low": 195.0, "close": 200.0}},
        atrs={"A2501": 2.0},
        config=config,
    )
    assert first_held_close.states["A"].highest_high == 205.0


def test_invalid_active_signal_atr_has_structured_context() -> None:
    config = small_config()
    signal_rows = pd.DataFrame(
        [
            {
                "trade_date": TRADE_DATE,
                "product": "A",
                "main_contract": "A2410",
                "main_close": 100.0,
                "atr": float("nan"),
                "strength": 1.0,
                "effective_direction": 1,
            }
        ]
    )

    with pytest.raises(SignalInputError) as exc_info:
        _close_plan(
            states={},
            signal_rows=signal_rows,
            bars={"A2410": {"high": 101.0, "low": 99.0, "close": 100.0}},
            atrs={"A2410": float("nan")},
            config=config,
        )

    error = exc_info.value
    assert error.trade_date == TRADE_DATE
    assert error.product == "A"
    assert error.contract == "A2410"
    assert error.check == "signal_atr"
    assert "signal_atr" in str(error)


def test_rebalance_target_requires_a_finite_positive_execution_open() -> None:
    with pytest.raises(ExecutionPriceError) as exc_info:
        _validate_target_opens(
            {},
            {"A2410": 0.25},
            {"A2410": float("nan")},
            trade_date=TRADE_DATE,
            contract_products={"A2410": "A"},
            context="formal_target",
        )

    assert exc_info.value.contract == "A2410"
    assert exc_info.value.product == "A"
    assert exc_info.value.context == "formal_target"
    assert pd.isna(exc_info.value.value)
    assert exc_info.value.reason == "rebalance price is non-finite"


def test_stateful_engine_validates_requested_date_range() -> None:
    data = make_carry_panel()

    with pytest.raises(ValueError, match="start"):
        CarryBacktester(
            data,
            small_config(),
            start=data.dates[10],
            end=data.dates[9],
        )
