"""Deterministic event-level accounting for minute Carry execution."""

from dataclasses import dataclass
from datetime import date, datetime
import math
from types import MappingProxyType
from typing import Mapping

from common.errors import EquityDepletedError

#: 复利化成本的浮点噪音上限。日收益量级是 1e−2，单次运算的 ulp 约 1e−18，一天几十
#: 个事件累计到 1e−16；真正为负的成本至少要大好几个数量级才可能出现。
_COST_NOISE = 1e-12


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """A direct-cost weight change at one portfolio event."""

    timestamp: datetime
    contract: str
    price: float
    old_weight: float
    new_weight: float
    weight_change: float
    turnover: float
    cost: float
    reason: str

    @property
    def direct_cost(self) -> float:
        return self.cost


@dataclass(frozen=True, slots=True)
class AccountEvent:
    """One simultaneous portfolio mark and optional rebalance."""

    timestamp: datetime
    event_type: str
    gross_return: float
    turnover: float
    cost: float
    net_return: float
    gross_equity: float
    equity: float
    gross_leverage: float
    executions: tuple[ExecutionRecord, ...]

    @property
    def direct_cost(self) -> float:
        return self.cost


@dataclass(frozen=True, slots=True)
class DailyAccountRow:
    """A close-marked daily ledger row derived from compounded equities."""

    trade_date: date
    timestamp: datetime
    opening_gross_equity: float
    opening_net_equity: float
    gross_return: float
    turnover: float
    cost: float
    direct_cost: float
    net_return: float
    gross_equity: float
    equity: float
    gross_leverage: float
    boundary_type: str


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{label} must be finite")
    return numeric


def _positive_price(value: object, contract: str) -> float:
    try:
        price = _finite(value, f"{contract} price")
    except ValueError as exc:
        raise ValueError(f"{contract} price must be finite and positive") from exc
    if price <= 0.0:
        raise ValueError(f"{contract} price must be finite and positive")
    return price


def _validate_contracts(mapping: Mapping[str, object], label: str) -> None:
    for contract in mapping:
        if not isinstance(contract, str) or not contract:
            raise ValueError(f"{label} contract must be a nonempty string")


def _validate_timestamp(timestamp: datetime) -> None:
    if not isinstance(timestamp, datetime):
        raise ValueError("timestamp must be a timezone-aware datetime")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")


class EventAccount:
    """Piecewise portfolio ledger with parallel gross and net equity."""

    def __init__(self, *, cost_bps: float) -> None:
        try:
            validated_cost = _finite(cost_bps, "cost_bps")
        except ValueError as exc:
            raise ValueError("cost_bps must be finite and nonnegative") from exc
        if validated_cost < 0.0:
            raise ValueError("cost_bps must be finite and nonnegative")

        self.cost_bps = validated_cost
        self._initialized = False
        self._weights: dict[str, float] = {}
        self._last_prices: dict[str, float] = {}
        self._last_timestamp: datetime | None = None
        self._gross_equity = 1.0
        self._net_equity = 1.0
        self._events: list[AccountEvent] = []
        self._executions: list[ExecutionRecord] = []
        self._daily_event_start = 0
        self._opening_gross_equity = 1.0
        self._opening_net_equity = 1.0
        self._pending_close: tuple[date, datetime] | None = None
        self._daily_opening_equities: dict[date, tuple[float, float]] = {}

    @property
    def equity(self) -> float:
        return self._net_equity

    @property
    def gross_equity(self) -> float:
        return self._gross_equity

    @property
    def weights(self) -> Mapping[str, float]:
        return MappingProxyType(dict(self._weights))

    @property
    def events(self) -> tuple[AccountEvent, ...]:
        return tuple(self._events)

    @property
    def executions(self) -> tuple[ExecutionRecord, ...]:
        return tuple(self._executions)

    @property
    def daily_opening_equities(
        self,
    ) -> Mapping[date, tuple[float, float]]:
        return MappingProxyType(dict(self._daily_opening_equities))

    def initialize(self, prices: Mapping[str, object]) -> None:
        if self._initialized:
            raise ValueError("account is already initialized")
        if not isinstance(prices, Mapping):
            raise ValueError("prices must be a mapping")
        _validate_contracts(prices, "price")
        self._last_prices = {
            contract: _positive_price(prices[contract], contract)
            for contract in sorted(prices)
        }
        self._initialized = True

    def rebalance(
        self,
        timestamp: datetime,
        prices: Mapping[str, object],
        target_weights: Mapping[str, object],
        reason_by_contract: Mapping[str, str],
    ) -> AccountEvent:
        return self._apply_event(
            timestamp=timestamp,
            prices=prices,
            target_weights=target_weights,
            reason_by_contract=reason_by_contract,
            event_type="rebalance",
        )

    def mark_close(
        self,
        trade_date: date,
        timestamp: datetime,
        prices: Mapping[str, object],
    ) -> AccountEvent:
        if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
            raise ValueError("trade_date must be a date")
        _validate_timestamp(timestamp)
        if timestamp.date() != trade_date:
            raise ValueError("trade_date must match the close timestamp date")

        event = self._apply_event(
            timestamp=timestamp,
            prices=prices,
            target_weights=self._weights,
            reason_by_contract={},
            event_type="close",
        )
        self._pending_close = (trade_date, timestamp)
        return event

    def drain_daily_row(
        self,
        trade_date: date,
        boundary_type: str,
    ) -> DailyAccountRow:
        self._require_initialized()
        if not isinstance(boundary_type, str) or not boundary_type:
            raise ValueError("boundary_type must be a nonempty string")
        if self._pending_close is None:
            raise ValueError("daily row requires a marked close")
        close_date, close_timestamp = self._pending_close
        if trade_date != close_date:
            raise ValueError(
                f"trade_date {trade_date} does not match marked close {close_date}"
            )
        if trade_date in self._daily_opening_equities:
            raise ValueError(f"trade_date {trade_date} was already drained")

        opening_gross = self._opening_gross_equity
        opening_net = self._opening_net_equity
        if (
            not math.isfinite(opening_gross)
            or not math.isfinite(opening_net)
            or opening_gross <= 0.0
            or opening_net <= 0.0
        ):
            raise ValueError("opening equities must be finite and positive")

        period_events = self._events[self._daily_event_start :]
        turnover = math.fsum(event.turnover for event in period_events)
        direct_cost = math.fsum(event.cost for event in period_events)
        gross_return = self._gross_equity / opening_gross - 1.0
        net_return = self._net_equity / opening_net - 1.0
        cost = gross_return - net_return
        # ⚠️ 零换手的日子里 gross 与 net 走的是**同一条相对路径**（net = k × gross，
        # k 是之前累计的成本因子），所以 `G/G₀ − 1` 与 `N/N₀ − 1` 在实数里恒等 ——
        # 在浮点里却能差出一两个 ulp，于是这里得到一个 −1e−16 的「负成本」并硬失败。
        # 把这一档噪音夹到 0；真正为负的成本仍然炸出去。
        if -_COST_NOISE < cost < 0.0:
            cost = 0.0
        gross_leverage = math.fsum(
            abs(self._weights[contract]) for contract in sorted(self._weights)
        )
        outputs = (
            turnover,
            direct_cost,
            gross_return,
            net_return,
            cost,
            gross_leverage,
            self._gross_equity,
            self._net_equity,
        )
        if not all(math.isfinite(value) for value in outputs):
            raise ValueError("daily account outputs must be finite")
        if turnover < 0.0 or direct_cost < 0.0 or cost < 0.0:
            raise ValueError("daily turnover and costs must be nonnegative")

        row = DailyAccountRow(
            trade_date=trade_date,
            timestamp=close_timestamp,
            opening_gross_equity=opening_gross,
            opening_net_equity=opening_net,
            gross_return=gross_return,
            turnover=turnover,
            cost=cost,
            direct_cost=direct_cost,
            net_return=net_return,
            gross_equity=self._gross_equity,
            equity=self._net_equity,
            gross_leverage=gross_leverage,
            boundary_type=boundary_type,
        )
        self._daily_opening_equities[trade_date] = (opening_gross, opening_net)
        self._opening_gross_equity = self._gross_equity
        self._opening_net_equity = self._net_equity
        self._daily_event_start = len(self._events)
        self._pending_close = None
        return row

    def _apply_event(
        self,
        *,
        timestamp: datetime,
        prices: Mapping[str, object],
        target_weights: Mapping[str, object],
        reason_by_contract: Mapping[str, str],
        event_type: str,
    ) -> AccountEvent:
        self._require_initialized()
        if self._pending_close is not None:
            raise ValueError("drain the marked close before adding another event")
        _validate_timestamp(timestamp)
        if self._last_timestamp is not None and timestamp <= self._last_timestamp:
            raise ValueError("timestamp must be strictly increasing")
        if not isinstance(prices, Mapping):
            raise ValueError("prices must be a mapping")
        if not isinstance(target_weights, Mapping):
            raise ValueError("target_weights must be a mapping")
        if not isinstance(reason_by_contract, Mapping):
            raise ValueError("reason_by_contract must be a mapping")
        _validate_contracts(prices, "price")
        _validate_contracts(target_weights, "weight")
        _validate_contracts(reason_by_contract, "reason")

        normalized_targets: dict[str, float] = {}
        for contract in sorted(target_weights):
            normalized_targets[contract] = _finite(
                target_weights[contract],
                f"{contract} weight",
            )

        contracts = sorted(set(self._weights) | set(normalized_targets))
        changed = [
            contract
            for contract in contracts
            if self._weights.get(contract, 0.0) != normalized_targets.get(contract, 0.0)
        ]
        required = sorted(
            {contract for contract, weight in self._weights.items() if weight != 0.0}
            | set(changed)
        )
        event_prices: dict[str, float] = {}
        for contract in required:
            if contract not in prices:
                raise ValueError(f"{contract} price is required")
            event_prices[contract] = _positive_price(prices[contract], contract)

        reasons: dict[str, str] = {}
        for contract in changed:
            reason = reason_by_contract.get(contract)
            if not isinstance(reason, str) or not reason:
                raise ValueError(f"{contract} reason is required for a weight change")
            reasons[contract] = reason

        contributions: list[float] = []
        for contract in sorted(self._weights):
            weight = self._weights[contract]
            if weight == 0.0:
                continue
            if contract not in self._last_prices:
                raise ValueError(f"{contract} previous price is required")
            previous_price = _positive_price(
                self._last_prices[contract],
                contract,
            )
            contribution = weight * (event_prices[contract] / previous_price - 1.0)
            if not math.isfinite(contribution):
                raise ValueError("gross return must be finite")
            contributions.append(contribution)
        try:
            gross_return = math.fsum(contributions)
        except OverflowError as exc:
            raise ValueError("gross return must be finite") from exc
        if not math.isfinite(gross_return):
            raise ValueError("gross return must be finite")

        changes = [
            abs(
                normalized_targets.get(contract, 0.0) - self._weights.get(contract, 0.0)
            )
            for contract in contracts
        ]
        try:
            turnover = math.fsum(changes)
        except OverflowError as exc:
            raise ValueError("turnover must be finite and nonnegative") from exc
        cost = turnover * self.cost_bps / 10_000.0
        net_return = gross_return - cost
        gross_equity = self._gross_equity * (1.0 + gross_return)
        net_equity = self._net_equity * (1.0 + net_return)
        gross_leverage = math.fsum(
            abs(normalized_targets[contract]) for contract in sorted(normalized_targets)
        )
        outputs = (
            turnover,
            cost,
            net_return,
            gross_equity,
            net_equity,
            gross_leverage,
        )
        if not all(math.isfinite(value) for value in outputs):
            raise ValueError("account event outputs must be finite")
        if turnover < 0.0 or cost < 0.0 or gross_leverage < 0.0:
            raise ValueError("account turnover, cost, and leverage must be nonnegative")
        if net_equity <= 0.0:
            raise EquityDepletedError(
                trade_date=timestamp.date(),
                previous_equity=self._net_equity,
                gross_return=gross_return,
                turnover=turnover,
                cost=cost,
                net_return=net_return,
                equity=net_equity,
            )

        executions = tuple(
            ExecutionRecord(
                timestamp=timestamp,
                contract=contract,
                price=event_prices[contract],
                old_weight=self._weights.get(contract, 0.0),
                new_weight=normalized_targets.get(contract, 0.0),
                weight_change=(
                    normalized_targets.get(contract, 0.0)
                    - self._weights.get(contract, 0.0)
                ),
                turnover=abs(
                    normalized_targets.get(contract, 0.0)
                    - self._weights.get(contract, 0.0)
                ),
                cost=(
                    abs(
                        normalized_targets.get(contract, 0.0)
                        - self._weights.get(contract, 0.0)
                    )
                    * self.cost_bps
                    / 10_000.0
                ),
                reason=reasons[contract],
            )
            for contract in changed
        )
        event = AccountEvent(
            timestamp=timestamp,
            event_type=event_type,
            gross_return=gross_return,
            turnover=turnover,
            cost=cost,
            net_return=net_return,
            gross_equity=gross_equity,
            equity=net_equity,
            gross_leverage=gross_leverage,
            executions=executions,
        )

        self._weights = {
            contract: normalized_targets[contract]
            for contract in sorted(normalized_targets)
            if normalized_targets[contract] != 0.0
        }
        self._last_prices = {
            contract: event_prices[contract] for contract in sorted(self._weights)
        }
        self._gross_equity = gross_equity
        self._net_equity = net_equity
        self._last_timestamp = timestamp
        self._events.append(event)
        self._executions.extend(executions)
        return event

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ValueError("account must be initialized")
