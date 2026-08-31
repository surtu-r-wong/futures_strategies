from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cta_carry.backtest import EquityDepletedError
from common.minute.account import AccountEvent, EventAccount, ExecutionRecord


TZ = ZoneInfo("Asia/Shanghai")


def _ts(hour: int, minute: int) -> datetime:
    return datetime(2024, 1, 8, hour, minute, tzinfo=TZ)


def test_piecewise_mark_rebalance_and_cost_identity() -> None:
    account = EventAccount(cost_bps=4.0)
    account.initialize({"RB2405.SHF": 100.0})

    first = account.rebalance(
        timestamp=_ts(9, 5),
        prices={"RB2405.SHF": 100.0},
        target_weights={"RB2405.SHF": 1.0},
        reason_by_contract={"RB2405.SHF": "entry"},
    )
    second = account.rebalance(
        timestamp=_ts(10, 35),
        prices={"RB2405.SHF": 110.0},
        target_weights={"RB2405.SHF": 2.0 / 3.0},
        reason_by_contract={"RB2405.SHF": "stop_1"},
    )
    close = account.mark_close(
        trade_date=date(2024, 1, 8),
        timestamp=_ts(15, 0),
        prices={"RB2405.SHF": 105.0},
    )
    daily = account.drain_daily_row(date(2024, 1, 8), "close")

    assert first.turnover == pytest.approx(1.0)
    assert second.gross_return == pytest.approx(0.10)
    assert second.turnover == pytest.approx(1.0 / 3.0)
    assert close.gross_return == pytest.approx((2.0 / 3.0) * (105.0 / 110.0 - 1.0))
    assert daily.net_return == pytest.approx(daily.gross_return - daily.cost)
    assert daily.cost >= 0.0
    assert account.equity > 0.0


def test_same_timestamp_contract_order_cannot_change_equity() -> None:
    left = EventAccount(cost_bps=4.0)
    right = EventAccount(cost_bps=4.0)
    prices = {"A": 100.0, "B": 200.0}
    left.initialize(prices)
    right.initialize(dict(reversed(tuple(prices.items()))))
    targets = {"A": 0.5, "B": -0.5}

    left.rebalance(_ts(9, 5), prices, targets, {"A": "entry", "B": "entry"})
    right.rebalance(
        _ts(9, 5),
        dict(reversed(tuple(prices.items()))),
        dict(reversed(tuple(targets.items()))),
        {"B": "entry", "A": "entry"},
    )
    next_prices = {"A": 110.0, "B": 180.0}
    next_targets = {"A": 0.25, "B": -0.75}
    left.rebalance(_ts(10, 35), next_prices, next_targets, {"A": "trim", "B": "add"})
    right.rebalance(
        _ts(10, 35),
        dict(reversed(tuple(next_prices.items()))),
        dict(reversed(tuple(next_targets.items()))),
        {"B": "add", "A": "trim"},
    )

    assert left.events == right.events
    assert left.executions == right.executions

    assert left.equity == right.equity
    assert left.gross_equity == right.gross_equity


def test_roll_turnover_and_execution_costs_use_sorted_contract_union() -> None:
    account = EventAccount(cost_bps=10.0)
    account.initialize({"A": 100.0})
    account.rebalance(_ts(9, 5), {"A": 100.0}, {"A": 1.0}, {"A": "entry"})

    event = account.rebalance(
        _ts(10, 35),
        {"B": 200.0, "A": 110.0},
        {"B": 1.0},
        {"B": "roll_new", "A": "roll_old"},
    )

    assert event.turnover == pytest.approx(2.0)
    assert event.cost == pytest.approx(0.002)
    assert tuple(record.contract for record in event.executions) == ("A", "B")
    assert event.executions == account.executions[-2:]
    assert sum(record.cost for record in event.executions) == pytest.approx(event.cost)


def test_daily_returns_use_parallel_compounded_equity_and_exact_cost() -> None:
    account = EventAccount(cost_bps=100.0)
    account.initialize({"A": 100.0})
    account.rebalance(_ts(9, 5), {"A": 100.0}, {"A": 1.0}, {"A": "entry"})
    account.rebalance(_ts(10, 35), {"A": 110.0}, {"A": 0.5}, {"A": "trim"})
    account.mark_close(date(2024, 1, 8), _ts(15, 0), {"A": 121.0})

    daily = account.drain_daily_row(date(2024, 1, 8), "close")

    expected_gross = 1.1 * 1.05
    expected_net = 0.99 * 1.095 * 1.05
    assert daily.opening_gross_equity == 1.0
    assert daily.opening_net_equity == 1.0
    assert daily.gross_equity == pytest.approx(expected_gross)
    assert daily.equity == pytest.approx(expected_net)
    assert daily.gross_return == pytest.approx(expected_gross - 1.0)
    assert daily.net_return == pytest.approx(expected_net - 1.0)
    assert daily.cost == daily.gross_return - daily.net_return
    assert daily.direct_cost == pytest.approx(0.015)
    assert daily.turnover == pytest.approx(1.5)
    assert daily.gross_leverage == pytest.approx(0.5)
    assert account.daily_opening_equities[date(2024, 1, 8)] == (1.0, 1.0)


def test_next_trade_date_retains_separate_opening_equities() -> None:
    account = EventAccount(cost_bps=10.0)
    account.initialize({"A": 100.0})
    account.rebalance(_ts(9, 5), {"A": 100.0}, {"A": 1.0}, {"A": "entry"})
    account.mark_close(date(2024, 1, 8), _ts(15, 0), {"A": 110.0})
    first = account.drain_daily_row(date(2024, 1, 8), "close")

    next_close = datetime(2024, 1, 9, 15, 0, tzinfo=TZ)
    account.mark_close(date(2024, 1, 9), next_close, {"A": 121.0})
    second = account.drain_daily_row(date(2024, 1, 9), "close")

    assert second.opening_gross_equity == first.gross_equity
    assert second.opening_net_equity == first.equity
    assert second.gross_return == pytest.approx(0.1)
    assert second.net_return == pytest.approx(0.1)
    assert second.cost == 0.0


def test_records_are_frozen() -> None:
    account = EventAccount(cost_bps=4.0)
    account.initialize({"A": 100.0})
    event = account.rebalance(_ts(9, 5), {"A": 100.0}, {"A": 1.0}, {"A": "entry"})

    assert isinstance(event, AccountEvent)
    assert isinstance(event.executions[0], ExecutionRecord)
    with pytest.raises(FrozenInstanceError):
        event.turnover = 2.0
    with pytest.raises(FrozenInstanceError):
        event.executions[0].cost = 2.0


def test_missing_price_for_held_or_changing_contract_is_rejected() -> None:
    account = EventAccount(cost_bps=4.0)
    account.initialize({"A": 100.0})
    account.rebalance(_ts(9, 5), {"A": 100.0}, {"A": 1.0}, {"A": "entry"})

    with pytest.raises(ValueError, match="A.*price"):
        account.rebalance(_ts(10, 35), {}, {"A": 0.5}, {"A": "trim"})
    with pytest.raises(ValueError, match="B.*price"):
        account.rebalance(
            _ts(10, 35),
            {"A": 110.0},
            {"A": 1.0, "B": 0.5},
            {"B": "entry"},
        )
    with pytest.raises(ValueError, match="A.*price"):
        account.mark_close(date(2024, 1, 8), _ts(15, 0), {})


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_nonpositive_or_nonfinite_required_price_is_rejected(price: float) -> None:
    account = EventAccount(cost_bps=4.0)
    with pytest.raises(ValueError, match="price.*finite and positive"):
        account.initialize({"A": price})


@pytest.mark.parametrize("weight", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_target_weight_is_rejected(weight: float) -> None:
    account = EventAccount(cost_bps=4.0)
    account.initialize({"A": 100.0})
    with pytest.raises(ValueError, match="weight.*finite"):
        account.rebalance(_ts(9, 5), {"A": 100.0}, {"A": weight}, {"A": "entry"})


@pytest.mark.parametrize("cost_bps", [-1.0, float("nan"), float("inf")])
def test_invalid_cost_is_rejected(cost_bps: float) -> None:
    with pytest.raises(ValueError, match="cost_bps.*finite and nonnegative"):
        EventAccount(cost_bps=cost_bps)


def test_duplicate_and_nonmonotonic_timestamps_are_rejected() -> None:
    account = EventAccount(cost_bps=4.0)
    account.initialize({"A": 100.0})
    account.rebalance(_ts(10, 35), {"A": 100.0}, {"A": 1.0}, {"A": "entry"})

    for timestamp in (_ts(10, 35), _ts(10, 34)):
        with pytest.raises(ValueError, match="timestamp.*strictly increasing"):
            account.rebalance(timestamp, {"A": 100.0}, {"A": 1.0}, {})


def test_naive_timestamp_is_rejected() -> None:
    account = EventAccount(cost_bps=4.0)
    account.initialize({"A": 100.0})
    with pytest.raises(ValueError, match="timestamp.*timezone-aware"):
        account.rebalance(
            datetime(2024, 1, 8, 9, 5),
            {"A": 100.0},
            {"A": 1.0},
            {"A": "entry"},
        )


def test_close_before_last_event_and_wrong_close_date_are_rejected() -> None:
    account = EventAccount(cost_bps=4.0)
    account.initialize({"A": 100.0})
    account.rebalance(_ts(10, 35), {"A": 100.0}, {"A": 1.0}, {"A": "entry"})

    with pytest.raises(ValueError, match="timestamp.*strictly increasing"):
        account.mark_close(date(2024, 1, 8), _ts(10, 34), {"A": 100.0})
    with pytest.raises(ValueError, match="trade_date.*timestamp"):
        account.mark_close(date(2024, 1, 9), _ts(15, 0), {"A": 100.0})


def test_missing_reason_for_a_changed_contract_is_rejected() -> None:
    account = EventAccount(cost_bps=4.0)
    account.initialize({"A": 100.0})
    with pytest.raises(ValueError, match="A.*reason"):
        account.rebalance(_ts(9, 5), {"A": 100.0}, {"A": 1.0}, {})


def test_drain_requires_matching_close_and_only_happens_once() -> None:
    account = EventAccount(cost_bps=4.0)
    account.initialize({"A": 100.0})

    with pytest.raises(ValueError, match="marked close"):
        account.drain_daily_row(date(2024, 1, 8), "close")
    account.mark_close(date(2024, 1, 8), _ts(15, 0), {})
    with pytest.raises(ValueError, match="trade_date"):
        account.drain_daily_row(date(2024, 1, 9), "close")
    account.drain_daily_row(date(2024, 1, 8), "close")
    with pytest.raises(ValueError, match="marked close"):
        account.drain_daily_row(date(2024, 1, 8), "close")


def test_events_cannot_be_added_after_close_before_daily_drain() -> None:
    account = EventAccount(cost_bps=4.0)
    account.initialize({})
    account.mark_close(date(2024, 1, 8), _ts(15, 0), {})
    with pytest.raises(ValueError, match="drain"):
        account.rebalance(_ts(15, 0) + timedelta(minutes=1), {}, {}, {})


def test_equity_depletion_raises_existing_structured_error_atomically() -> None:
    account = EventAccount(cost_bps=10_000.0)
    account.initialize({"A": 100.0})

    with pytest.raises(EquityDepletedError) as exc_info:
        account.rebalance(_ts(9, 5), {"A": 100.0}, {"A": 1.0}, {"A": "entry"})

    error = exc_info.value
    assert error.trade_date == date(2024, 1, 8)
    assert error.previous_equity == 1.0
    assert error.turnover == 1.0
    assert error.cost == 1.0
    assert error.equity == 0.0
    assert account.equity == 1.0
    assert account.events == ()


def test_initialize_and_boundary_type_are_validated() -> None:
    account = EventAccount(cost_bps=4.0)
    account.initialize({})
    with pytest.raises(ValueError, match="already initialized"):
        account.initialize({})
    account.mark_close(date(2024, 1, 8), _ts(15, 0), {})
    with pytest.raises(ValueError, match="boundary_type"):
        account.drain_daily_row(date(2024, 1, 8), "")


def test_a_zero_turnover_day_does_not_fail_on_floating_point_noise():
    """零换手的日子里复利化成本可能是个 −1e−16，那不是负成本，是浮点噪音。

    没有成交时 gross 与 net 走同一条相对路径（`net = k × gross`，k 是此前累计的
    成本因子），所以 `G/G₀ − 1` 与 `N/N₀ − 1` 在实数里恒等；浮点里差一两个 ulp，
    旧口径会把整段回测炸掉。下面这组价格是搜出来的**真实**触发例，不是构造的
    近似 —— 去掉夹取它就报 `daily turnover and costs must be nonnegative`。

    接线 `cta_continuous.backtest` 时碰到的：连续信号策略在闸关着的日子完全不换仓，
    这种一天零成交的情形远比 Carry 那条线常见。
    """
    shanghai = ZoneInfo("Asia/Shanghai")
    prices = (1002.0, 1000.0, 1004.0, 998.0)
    account = EventAccount(cost_bps=1.3)
    account.initialize({"RB2405": prices[0]})

    first = datetime(2024, 3, 5, 9, 15, tzinfo=shanghai)
    account.rebalance(
        first, {"RB2405": prices[0]}, {"RB2405": 3.0}, {"RB2405": "entry"}
    )
    for offset, price in enumerate(prices[1:], start=1):
        account.rebalance(
            first.replace(minute=15 + offset), {"RB2405": price}, {"RB2405": 3.0}, {}
        )
    account.mark_close(date(2024, 3, 5), first.replace(hour=15), {"RB2405": prices[-1]})
    account.drain_daily_row(date(2024, 3, 5), boundary_type="session")

    second = datetime(2024, 3, 6, 9, 15, tzinfo=shanghai)
    for offset, price in enumerate(prices):
        account.rebalance(
            second.replace(minute=15 + offset), {"RB2405": price}, {"RB2405": 3.0}, {}
        )
    account.mark_close(date(2024, 3, 6), second.replace(hour=15), {"RB2405": prices[-1]})
    row = account.drain_daily_row(date(2024, 3, 6), boundary_type="session")

    assert row.turnover == 0.0
    assert row.cost == 0.0
    assert row.net_return == pytest.approx(row.gross_return, abs=1e-15)
