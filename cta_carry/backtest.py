"""Pure contract-level accounting primitives for Carry backtests."""
from dataclasses import dataclass, field
from datetime import date
import math

import pandas as pd


_MISSING = object()


class ExecutionPriceError(RuntimeError):
    def __init__(
        self,
        trade_date,
        contract,
        check,
        reason,
    ) -> None:
        self.trade_date = trade_date
        self.contract = contract
        self.check = check
        self.reason = reason
        super().__init__(
            f"{trade_date} {contract} {check}: {reason}"
        )


class WarmupInsufficientError(RuntimeError):
    def __init__(
        self,
        *,
        query_start,
        report_start_date,
        signal_ready_date,
        shadow_observations,
        active_days,
        required_observations,
        required_active_days,
    ) -> None:
        self.query_start = query_start
        self.report_start_date = report_start_date
        self.signal_ready_date = signal_ready_date
        self.shadow_observations = shadow_observations
        self.active_days = active_days
        self.required_observations = required_observations
        self.required_active_days = required_active_days
        self.shadow_gap = max(
            0,
            required_observations - shadow_observations,
        )
        self.active_gap = max(
            0,
            required_active_days - active_days,
        )
        super().__init__(
            "risk scaling not ready: "
            f"query_start={query_start}, "
            f"report_start_date={report_start_date}, "
            f"signal_ready_date={signal_ready_date}, "
            f"shadow={shadow_observations}/{required_observations}, "
            f"active={active_days}/{required_active_days}"
        )


def _finite_float(value, name) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be finite")
    return numeric


def _required_open(
    prices,
    contract,
    *,
    period,
    trade_date,
) -> float:
    value = prices.get(contract, _MISSING)
    if value is _MISSING:
        raise ExecutionPriceError(
            trade_date,
            contract,
            "open_price",
            f"{period} open is missing",
        )
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        price = float("nan")
    if not math.isfinite(price) or price <= 0.0:
        raise ExecutionPriceError(
            trade_date,
            contract,
            "open_price",
            f"{period} open must be finite and positive",
        )
    return price


def contract_gross_return(
    weights,
    previous_open,
    current_open,
    *,
    trade_date,
) -> float:
    """Return the open-to-open PnL of held concrete contracts."""
    contributions = []
    for contract, raw_weight in weights.items():
        weight = _finite_float(raw_weight, "weight")
        if weight == 0.0:
            continue
        previous_price = _required_open(
            previous_open,
            contract,
            period="previous",
            trade_date=trade_date,
        )
        current_price = _required_open(
            current_open,
            contract,
            period="current",
            trade_date=trade_date,
        )
        try:
            contribution = weight * (
                current_price / previous_price - 1.0
            )
        except (FloatingPointError, OverflowError) as exc:
            raise ValueError("gross return must be finite") from exc
        if not math.isfinite(contribution):
            raise ValueError("gross return must be finite")
        contributions.append(contribution)

    try:
        gross_return = math.fsum(contributions)
    except OverflowError as exc:
        raise ValueError("gross return must be finite") from exc
    if not math.isfinite(gross_return):
        raise ValueError("gross return must be finite")
    return gross_return


def weight_turnover(old_weights, new_weights) -> float:
    """Return one-way absolute weight changes over the contract union."""
    contracts = dict.fromkeys(
        [*old_weights.keys(), *new_weights.keys()]
    )
    changes = []
    for contract in contracts:
        old_weight = _finite_float(
            old_weights.get(contract, 0.0),
            "weight",
        )
        new_weight = _finite_float(
            new_weights.get(contract, 0.0),
            "weight",
        )
        change = abs(new_weight - old_weight)
        if not math.isfinite(change):
            raise ValueError("turnover must be finite and nonnegative")
        changes.append(change)

    try:
        turnover = math.fsum(changes)
    except OverflowError as exc:
        raise ValueError(
            "turnover must be finite and nonnegative"
        ) from exc
    if not math.isfinite(turnover) or turnover < 0.0:
        raise ValueError("turnover must be finite and nonnegative")
    return turnover


def _ledger_row(
    *,
    trade_date,
    gross_return,
    turnover,
    cost,
    net_return,
    equity,
    boundary_type,
):
    outputs = (
        gross_return,
        turnover,
        cost,
        net_return,
        equity,
    )
    if not all(math.isfinite(value) for value in outputs):
        raise ValueError("ledger outputs must be finite")
    return {
        "trade_date": trade_date,
        "gross_return": gross_return,
        "turnover": turnover,
        "cost": cost,
        "net_return": net_return,
        "equity": equity,
        "boundary_type": boundary_type,
    }


def ordinary_ledger_row(
    *,
    trade_date,
    previous_equity,
    gross_return,
    turnover,
    cost_bps,
):
    """Build an ordinary open-to-open accounting row."""
    previous_equity = _finite_float(
        previous_equity,
        "previous_equity",
    )
    gross_return = _finite_float(
        gross_return,
        "gross_return",
    )
    turnover = _finite_float(turnover, "turnover")
    cost_bps = _finite_float(cost_bps, "cost_bps")
    if turnover < 0.0:
        raise ValueError("turnover must be nonnegative")
    if cost_bps < 0.0:
        raise ValueError("cost_bps must be nonnegative")

    cost = turnover * cost_bps / 10_000.0
    net_return = gross_return - cost
    equity = previous_equity * (1.0 + net_return)
    return _ledger_row(
        trade_date=trade_date,
        gross_return=gross_return,
        turnover=turnover,
        cost=cost,
        net_return=net_return,
        equity=equity,
        boundary_type="ordinary",
    )


def initial_report_row(
    *,
    trade_date,
    carried_weights,
    target_weights,
    cost_bps,
):
    """Reset report equity and charge only the opening rebalance."""
    cost_bps = _finite_float(cost_bps, "cost_bps")
    if cost_bps < 0.0:
        raise ValueError("cost_bps must be nonnegative")

    turnover = weight_turnover(
        carried_weights,
        target_weights,
    )
    cost = turnover * cost_bps / 10_000.0
    net_return = -cost
    equity = 1.0 - cost
    return _ledger_row(
        trade_date=trade_date,
        gross_return=0.0,
        turnover=turnover,
        cost=cost,
        net_return=net_return,
        equity=equity,
        boundary_type="report_start_initialization",
    )


@dataclass(frozen=True)
class CarryBacktestResult:
    daily_returns: pd.DataFrame
    positions: pd.DataFrame
    trades: pd.DataFrame
    signals: pd.DataFrame
    curve_selection: pd.DataFrame
    data_quality: pd.DataFrame
    run_config: pd.DataFrame
    metrics: dict = field(default_factory=dict)
