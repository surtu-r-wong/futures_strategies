from dataclasses import FrozenInstanceError
from datetime import date

import pandas as pd
import pytest

from cta_carry.backtest import (
    CarryBacktestResult,
    ExecutionPriceError,
    WarmupInsufficientError,
    contract_gross_return,
    initial_report_row,
    ordinary_ledger_row,
    weight_turnover,
)


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
    assert str(error) == (
        "2024-01-03 A open_price: current open is missing"
    )


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

    assert exc_info.value.reason == (
        "current open must be finite and positive"
    )


def test_zero_weight_contract_does_not_require_open_prices() -> None:
    assert contract_gross_return(
        {"A": 0.0},
        {},
        {},
        trade_date=TRADE_DATE,
    ) == 0.0


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
    assert row["equity"] == pytest.approx(
        2.0 * (1.0 + row["net_return"])
    )


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
def test_contract_gross_return_rejects_nonfinite_weights(
    weights, message
) -> None:
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
def test_weight_turnover_rejects_nonfinite_weights(
    old_weights, new_weights
) -> None:
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
