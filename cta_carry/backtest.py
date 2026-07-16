"""Contract accounting primitives and the stateful Carry daily engine."""

from dataclasses import asdict, dataclass, field
from datetime import date
import math

import pandas as pd

from common.metrics import summarize

from .config import CarryConfig
from .curve import build_curve
from .data import CarryDataSet
from .risk import (
    PositionState,
    ShadowVolWindow,
    apply_chandelier,
    compute_contract_atr,
    raw_target_weight,
    scale_weights,
    transition_signal,
)
from .signals import build_signals


_MISSING = object()
_DAILY_COLUMNS = (
    "trade_date",
    "gross_return",
    "turnover",
    "cost",
    "net_return",
    "equity",
    "gross_leverage",
    "boundary_type",
)
_POSITION_COLUMNS = (
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
)
_TRADE_COLUMNS = (
    "trade_date",
    "product",
    "contract",
    "old_weight",
    "new_weight",
    "weight_change",
    "reason",
)
_RUN_CONFIG_COLUMNS = ("key", "value")


class ExecutionPriceError(RuntimeError):
    def __init__(
        self,
        trade_date,
        contract,
        check,
        reason,
        *,
        product=None,
        context=None,
        value=None,
    ) -> None:
        self.trade_date = trade_date
        self.product = product
        self.contract = contract
        self.check = check
        self.reason = reason
        self.context = context
        self.value = value
        message = f"{trade_date} {contract} {check}: {reason}"
        details = []
        if product is not None:
            details.append(f"product={product}")
        if context is not None:
            details.append(f"context={context}")
        if value is not None:
            details.append(f"value={value!r}")
        if details:
            message += " [" + ", ".join(details) + "]"
        super().__init__(message)


class SignalInputError(RuntimeError):
    def __init__(
        self,
        *,
        trade_date,
        product,
        contract,
        check,
        reason,
        value=None,
    ) -> None:
        self.trade_date = trade_date
        self.product = product
        self.contract = contract
        self.check = check
        self.reason = reason
        self.value = value
        super().__init__(
            f"{trade_date} {product} {contract} {check}: {reason}; value={value!r}"
        )


class EquityDepletedError(RuntimeError):
    def __init__(
        self,
        *,
        trade_date,
        previous_equity,
        gross_return,
        turnover,
        cost,
        net_return,
        equity,
    ) -> None:
        self.trade_date = trade_date
        self.previous_equity = previous_equity
        self.gross_return = gross_return
        self.turnover = turnover
        self.cost = cost
        self.net_return = net_return
        self.equity = equity
        super().__init__(
            f"{trade_date} equity depleted: previous_equity={previous_equity}, "
            f"gross_return={gross_return}, turnover={turnover}, cost={cost}, "
            f"net_return={net_return}, equity={equity}"
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


@dataclass(frozen=True)
class ClosePlan:
    states: dict[str, PositionState]
    raw_weights: dict[str, float]
    reasons: dict[str, str]


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
    product=None,
    context=None,
) -> float:
    value = prices.get(contract, _MISSING)
    if value is _MISSING:
        raise ExecutionPriceError(
            trade_date,
            contract,
            "open_price",
            f"{period} open is missing",
            product=product,
            context=context,
        )
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        price = float("nan")
    if not math.isfinite(price) or price <= 0.0:
        if context is None:
            reason = f"{period} open must be finite and positive"
        elif not math.isfinite(price):
            reason = f"{period} open is non-finite"
        else:
            reason = f"{period} open is non-positive"
        raise ExecutionPriceError(
            trade_date,
            contract,
            "open_price",
            reason,
            product=product,
            context=context,
            value=value,
        )
    return price


def contract_gross_return(
    weights,
    previous_open,
    current_open,
    *,
    trade_date,
    contract_products=None,
    context=None,
) -> float:
    """Return the open-to-open PnL of held concrete contracts."""
    products = contract_products or {}
    contributions = []
    for contract, raw_weight in weights.items():
        weight = _finite_float(raw_weight, "weight")
        if weight == 0.0:
            continue
        product = products.get(contract)
        previous_price = _required_open(
            previous_open,
            contract,
            period="previous",
            trade_date=trade_date,
            product=product,
            context=context,
        )
        current_price = _required_open(
            current_open,
            contract,
            period="current",
            trade_date=trade_date,
            product=product,
            context=context,
        )
        try:
            contribution = weight * (current_price / previous_price - 1.0)
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
    contracts = dict.fromkeys([*old_weights.keys(), *new_weights.keys()])
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
        raise ValueError("turnover must be finite and nonnegative") from exc
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
    if previous_equity <= 0.0:
        raise ValueError("previous_equity must be greater than 0")
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
    if math.isfinite(equity) and equity <= 0.0:
        raise EquityDepletedError(
            trade_date=trade_date,
            previous_equity=previous_equity,
            gross_return=gross_return,
            turnover=turnover,
            cost=cost,
            net_return=net_return,
            equity=equity,
        )
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
    if math.isfinite(equity) and equity <= 0.0:
        raise EquityDepletedError(
            trade_date=trade_date,
            previous_equity=1.0,
            gross_return=0.0,
            turnover=turnover,
            cost=cost,
            net_return=net_return,
            equity=equity,
        )
    return _ledger_row(
        trade_date=trade_date,
        gross_return=0.0,
        turnover=turnover,
        cost=cost,
        net_return=net_return,
        equity=equity,
        boundary_type="report_start_initialization",
    )


def _curve_with_atr(prices: pd.DataFrame, config: CarryConfig):
    """Build the curve, attach main-contract ATR, and construct signals."""
    curve_result = build_curve(prices, config)
    contract_atr = compute_contract_atr(prices, config)
    main_atr = contract_atr.loc[:, ["trade_date", "contract", "atr"]].rename(
        columns={"contract": "main_contract"}
    )
    curve_with_atr = curve_result.curve.merge(
        main_atr,
        on=["trade_date", "main_contract"],
        how="left",
        validate="one_to_one",
    )
    signal_result = build_signals(curve_with_atr, config)
    return curve_result, contract_atr, signal_result


def _bar_maps(day_prices: pd.DataFrame):
    """Build execution and OHLC lookups for exactly one trading day."""
    dates = day_prices["trade_date"].drop_duplicates()
    if len(dates) != 1:
        raise ValueError("bar lookup requires exactly one trade date")
    opens: dict[str, float] = {}
    bars: dict[str, dict[str, float]] = {}
    ordered = day_prices.sort_values(["product", "contract"], kind="mergesort")
    for row in ordered.itertuples(index=False):
        opens[row.contract] = row.open
        bars[row.contract] = {
            "high": row.high,
            "low": row.low,
            "close": row.close,
        }
    return opens, bars


def _atr_map(day_atr: pd.DataFrame) -> dict[str, float]:
    """Build an ATR lookup for exactly one trading day."""
    dates = day_atr["trade_date"].drop_duplicates()
    if len(dates) != 1:
        raise ValueError("ATR lookup requires exactly one trade date")
    return {
        row.contract: row.atr
        for row in day_atr.sort_values("contract", kind="mergesort").itertuples(
            index=False
        )
    }


def _contract_products(prices: pd.DataFrame) -> dict[str, str]:
    pairs = prices.loc[:, ["contract", "product"]].drop_duplicates()
    if pairs["contract"].duplicated(keep=False).any():
        raise ValueError("one contract cannot map to multiple products")
    return pairs.set_index("contract")["product"].to_dict()


def _valid_positive(value) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0.0
    except (TypeError, ValueError, OverflowError):
        return False


def _close_plan(
    states: dict[str, PositionState],
    signal_rows: pd.DataFrame,
    bars: dict[str, dict[str, float]],
    atrs: dict[str, float],
    config: CarryConfig,
) -> ClosePlan:
    """Apply stops, signal transitions, and raw risk sizing at the close."""
    signals = {
        row.product: row
        for row in signal_rows.sort_values("product", kind="mergesort").itertuples(
            index=False
        )
    }
    products = sorted(set(states) | set(signals))
    next_states: dict[str, PositionState] = {}
    raw_weights: dict[str, float] = {}
    reasons: dict[str, str] = {}

    for product in products:
        before = states.get(product, PositionState())
        after_stop = before
        stop_triggered = False
        if before.direction != 0 and before.contract is not None:
            bar = bars.get(before.contract)
            atr = atrs.get(before.contract)
            if bar is not None and _valid_positive(atr):
                after_stop, stop_triggered = apply_chandelier(
                    before,
                    bar["high"],
                    bar["low"],
                    bar["close"],
                    atr,
                    config,
                )

        signal = signals.get(product)
        direction = int(signal.effective_direction) if signal is not None else 0
        contract = signal.main_contract if direction != 0 else None
        after = transition_signal(after_stop, direction, contract, config)

        old_direction = (
            before.direction if before.direction != 0 else before.locked_direction
        )
        if old_direction != 0 and after.direction == -old_direction:
            reason = "direction_reversal"
        elif stop_triggered:
            stage = config.stop_tranches - after_stop.tranches_remaining
            reason = f"stop_{stage}"
        elif before.direction != 0 and after.direction == 0:
            reason = "signal_exit"
        elif before.direction == 0 and after.direction != 0:
            reason = "entry"
        elif (
            before.direction == after.direction != 0
            and before.contract != after.contract
        ):
            reason = "roll"
        else:
            reason = "rebalance"

        next_states[product] = after
        reasons[product] = reason
        if after.direction != 0 and after.contract is not None:
            signal_date = getattr(signal, "trade_date", None)
            signal_atr = getattr(signal, "atr", None)
            if signal is None or not _valid_positive(signal_atr):
                raise SignalInputError(
                    trade_date=signal_date,
                    product=product,
                    contract=after.contract,
                    check="signal_atr",
                    reason="active target requires finite positive ATR",
                    value=signal_atr,
                )
            try:
                raw_weights[after.contract] = raw_target_weight(
                    after.direction,
                    float(signal.strength),
                    float(signal.main_close),
                    float(signal.atr),
                    after.tranches_remaining,
                    config,
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise SignalInputError(
                    trade_date=signal_date,
                    product=product,
                    contract=after.contract,
                    check="target_sizing",
                    reason=str(exc),
                    value={
                        "strength": getattr(signal, "strength", None),
                        "close": getattr(signal, "main_close", None),
                        "atr": signal_atr,
                    },
                ) from exc

    return ClosePlan(
        states=next_states,
        raw_weights=raw_weights,
        reasons=reasons,
    )


def _validate_target_opens(
    old_weights,
    target_weights,
    opens,
    *,
    trade_date,
    contract_products=None,
    context=None,
) -> None:
    """Require a valid current open for every contract being rebalanced."""
    products = contract_products or {}
    for contract in sorted(set(old_weights) | set(target_weights)):
        old_weight = _finite_float(old_weights.get(contract, 0.0), "weight")
        target_weight = _finite_float(target_weights.get(contract, 0.0), "weight")
        if old_weight == target_weight:
            continue
        value = opens.get(contract, _MISSING)
        try:
            price = float(value)
        except (TypeError, ValueError, OverflowError):
            price = float("nan")
        if value is _MISSING:
            reason = "rebalance price is missing"
        elif not math.isfinite(price):
            reason = "rebalance price is non-finite"
        elif price <= 0.0:
            reason = "rebalance price is non-positive"
        else:
            continue
        if context is None:
            reason = "rebalance price"
        raise ExecutionPriceError(
            trade_date,
            contract,
            "open_price",
            reason,
            product=products.get(contract),
            context=context,
            value=None if value is _MISSING else value,
        )


@dataclass(frozen=True)
class CarryBacktestResult:
    """Shallowly frozen result bundle; contained frames remain caller-mutable."""

    daily_returns: pd.DataFrame
    positions: pd.DataFrame
    trades: pd.DataFrame
    signals: pd.DataFrame
    curve_selection: pd.DataFrame
    data_quality: pd.DataFrame
    run_config: pd.DataFrame
    metrics: dict = field(default_factory=dict)


def _gross_leverage(weights: dict[str, float]) -> float:
    values = [abs(_finite_float(value, "weight")) for value in weights.values()]
    try:
        gross = math.fsum(values)
    except OverflowError as exc:
        raise ValueError("gross leverage must be finite") from exc
    if not math.isfinite(gross):
        raise ValueError("gross leverage must be finite")
    return gross


def _trade_rows(
    *,
    trade_date: date,
    old_weights: dict[str, float],
    new_weights: dict[str, float],
    contract_products: dict[str, str],
    reasons: dict[str, str],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for contract in sorted(set(old_weights) | set(new_weights)):
        old_weight = _finite_float(old_weights.get(contract, 0.0), "weight")
        new_weight = _finite_float(new_weights.get(contract, 0.0), "weight")
        if old_weight == new_weight:
            continue
        product = contract_products.get(contract)
        if product is None:
            raise ValueError(f"unknown product for contract {contract}")
        rows.append(
            {
                "trade_date": trade_date,
                "product": product,
                "contract": contract,
                "old_weight": old_weight,
                "new_weight": new_weight,
                "weight_change": new_weight - old_weight,
                "reason": reasons.get(product, "rebalance"),
            }
        )
    return rows


def _position_rows(
    *,
    trade_date: date,
    states: dict[str, PositionState],
    raw_weights: dict[str, float],
    formal_weights: dict[str, float],
    carried_weights: dict[str, float],
    report_start_date: date,
) -> list[dict[str, object]]:
    gross = _gross_leverage(formal_weights)
    rows: list[dict[str, object]] = []
    for product, state in sorted(states.items()):
        if state.direction == 0 and state.locked_direction == 0:
            continue
        contract = state.contract
        raw_weight = (
            _finite_float(raw_weights.get(contract, 0.0), "weight")
            if contract is not None
            else 0.0
        )
        formal_weight = (
            _finite_float(formal_weights.get(contract, 0.0), "weight")
            if contract is not None
            else 0.0
        )
        rows.append(
            {
                "trade_date": trade_date,
                "product": product,
                "contract": contract,
                "direction": state.direction,
                "raw_weight": raw_weight,
                "weight": formal_weight,
                "gross_leverage": gross,
                "tranches_remaining": state.tranches_remaining,
                "highest_high": state.highest_high,
                "lowest_low": state.lowest_low,
                "locked_direction": state.locked_direction,
                "carried_in": (
                    trade_date == report_start_date
                    and contract is not None
                    and carried_weights.get(contract, 0.0) != 0.0
                ),
            }
        )
    return rows


def _records_frame(records, columns) -> pd.DataFrame:
    return pd.DataFrame.from_records(records, columns=list(columns)).reset_index(
        drop=True
    )


def _summary_metrics(daily: pd.DataFrame) -> dict[str, float]:
    indexed = daily.set_index("trade_date")
    metrics = summarize(
        indexed["net_return"],
        periods_per_year=252,
        turnover=indexed["turnover"],
    )
    max_drawdown = metrics["max_drawdown"]
    metrics.update(
        {
            "calmar": (
                metrics["ann_return"] / max_drawdown
                if math.isfinite(max_drawdown) and max_drawdown > 0.0
                else float("nan")
            ),
            "total_cost": float(daily["cost"].sum()),
            "avg_gross_leverage": float(daily["gross_leverage"].mean()),
            "max_gross_leverage": float(daily["gross_leverage"].max()),
        }
    )
    return metrics


class CarryBacktester:
    """Run the close-to-next-open Carry strategy with one discrete state."""

    def __init__(
        self,
        data: CarryDataSet,
        config: CarryConfig,
        *,
        start: date,
        end: date,
    ) -> None:
        if start > end:
            raise ValueError("start must be on or before end")
        self.data = data
        self.config = config
        self.start = start
        self.end = end

    def run(self) -> CarryBacktestResult:
        prices = self.data.prices.loc[self.data.prices["trade_date"] <= self.end].copy()
        dates = sorted(prices["trade_date"].dropna().unique().tolist())
        if not dates:
            raise ValueError("no Carry prices on or before end")
        report_dates = [trade_date for trade_date in dates if trade_date >= self.start]
        if not report_dates:
            raise ValueError("no strategy trading day on or after start")
        query_start = dates[0]
        report_start_date = report_dates[0]

        curve_result, contract_atr, signal_result = _curve_with_atr(prices, self.config)
        contract_products = _contract_products(prices)
        price_groups = iter(prices.groupby("trade_date", sort=True))
        atr_groups = iter(contract_atr.groupby("trade_date", sort=True))
        signal_groups = iter(signal_result.signals.groupby("trade_date", sort=True))
        next_signal_group = next(signal_groups, None)
        empty_signals = pd.DataFrame(columns=signal_result.signals.columns)

        states: dict[str, PositionState] = {}
        raw_weights: dict[str, float] = {}
        formal_weights: dict[str, float] = {}
        pending_raw: dict[str, float] = {}
        pending_formal: dict[str, float] = {}
        pending_reasons: dict[str, str] = {}
        pending_source_date: date | None = None
        pending_scale_ready = False

        shadow = ShadowVolWindow(self.config)
        pending_estimate = shadow.estimate()
        shadow_interval_enabled = False
        previous_open: dict[str, float] | None = None
        vol_ready_date: date | None = None
        equity: float | None = None

        daily_records: list[dict[str, object]] = []
        position_records: list[dict[str, object]] = []
        trade_records: list[dict[str, object]] = []

        daily_groups = zip(price_groups, atr_groups, strict=True)
        for index, grouped_day in enumerate(daily_groups):
            (trade_date, day_prices), (atr_date, day_atr) = grouped_day
            if trade_date != atr_date or trade_date != dates[index]:
                raise ValueError("price and ATR trading dates must align")
            current_open, bars = _bar_maps(day_prices)
            atrs = _atr_map(day_atr)
            day_signals = empty_signals
            if next_signal_group is not None:
                signal_date, signal_frame = next_signal_group
                if signal_date < trade_date:
                    raise ValueError("signal trading dates must be ordered")
                if signal_date == trade_date:
                    day_signals = signal_frame
                    next_signal_group = next(signal_groups, None)
            carried_formal = dict(formal_weights)

            raw_gross = 0.0
            formal_gross = 0.0
            if previous_open is not None:
                raw_gross = contract_gross_return(
                    raw_weights,
                    previous_open,
                    current_open,
                    trade_date=trade_date,
                    contract_products=contract_products,
                    context="shadow_raw_holdings",
                )
                formal_gross = contract_gross_return(
                    formal_weights,
                    previous_open,
                    current_open,
                    trade_date=trade_date,
                    contract_products=contract_products,
                    context="formal_holdings",
                )

            target_raw = dict(pending_raw)
            target_formal = dict(pending_formal)
            _validate_target_opens(
                raw_weights,
                target_raw,
                current_open,
                trade_date=trade_date,
                contract_products=contract_products,
                context="shadow_raw_target",
            )
            _validate_target_opens(
                formal_weights,
                target_formal,
                current_open,
                trade_date=trade_date,
                contract_products=contract_products,
                context="formal_target",
            )
            raw_turnover = weight_turnover(raw_weights, target_raw)
            formal_turnover = weight_turnover(formal_weights, target_formal)

            if previous_open is not None and shadow_interval_enabled:
                shadow.append(
                    raw_gross - raw_turnover * self.config.cost_bps / 10_000.0,
                    active=_gross_leverage(raw_weights) > 0.0,
                )

            if trade_date == report_start_date and not pending_scale_ready:
                raise WarmupInsufficientError(
                    query_start=query_start,
                    report_start_date=report_start_date,
                    signal_ready_date=signal_result.signal_ready_date,
                    shadow_observations=pending_estimate.observations,
                    active_days=pending_estimate.active_days,
                    required_observations=self.config.vol_window,
                    required_active_days=self.config.min_shadow_active_days,
                )

            raw_weights = target_raw
            formal_weights = target_formal
            formal_gross_leverage = _gross_leverage(formal_weights)

            if trade_date == report_start_date:
                ledger = initial_report_row(
                    trade_date=trade_date,
                    carried_weights=carried_formal,
                    target_weights=formal_weights,
                    cost_bps=self.config.cost_bps,
                )
                equity = float(ledger["equity"])
            elif trade_date > report_start_date:
                ledger = ordinary_ledger_row(
                    trade_date=trade_date,
                    previous_equity=float(equity),
                    gross_return=formal_gross,
                    turnover=formal_turnover,
                    cost_bps=self.config.cost_bps,
                )
                equity = float(ledger["equity"])
            else:
                ledger = None

            if ledger is not None:
                ledger["gross_leverage"] = formal_gross_leverage
                daily_records.append(ledger)
                trade_records.extend(
                    _trade_rows(
                        trade_date=trade_date,
                        old_weights=carried_formal,
                        new_weights=formal_weights,
                        contract_products=contract_products,
                        reasons=pending_reasons,
                    )
                )
                position_records.extend(
                    _position_rows(
                        trade_date=trade_date,
                        states=states,
                        raw_weights=raw_weights,
                        formal_weights=formal_weights,
                        carried_weights=carried_formal,
                        report_start_date=report_start_date,
                    )
                )

            shadow_interval_enabled = (
                pending_source_date is not None
                and signal_result.signal_ready_date is not None
                and pending_source_date >= signal_result.signal_ready_date
            )
            previous_open = current_open
            if index == len(dates) - 1:
                continue

            plan = _close_plan(
                states,
                day_signals,
                bars,
                atrs,
                self.config,
            )
            states = plan.states
            pending_raw = plan.raw_weights
            pending_reasons = plan.reasons
            pending_source_date = trade_date

            estimate = shadow.estimate()
            pending_estimate = estimate
            if estimate.ready:
                pending_formal = scale_weights(
                    pending_raw,
                    estimate.vol_scale,
                    self.config,
                )
                pending_scale_ready = True
                if vol_ready_date is None:
                    vol_ready_date = dates[index + 1]
            else:
                pending_formal = {}
                pending_scale_ready = False

        daily = _records_frame(daily_records, _DAILY_COLUMNS)
        positions = _records_frame(position_records, _POSITION_COLUMNS)
        trades = _records_frame(trade_records, _TRADE_COLUMNS)
        config_rows: list[dict[str, object]] = [
            {"key": "requested_start", "value": self.start},
            {"key": "requested_end", "value": self.end},
            {"key": "query_start", "value": query_start},
            {"key": "report_start_date", "value": report_start_date},
            {
                "key": "signal_ready_date",
                "value": signal_result.signal_ready_date,
            },
            {"key": "vol_ready_date", "value": vol_ready_date},
        ]
        config_rows.extend(
            {"key": key, "value": value} for key, value in asdict(self.config).items()
        )
        return CarryBacktestResult(
            daily_returns=daily,
            positions=positions,
            trades=trades,
            signals=signal_result.signals.reset_index(drop=True),
            curve_selection=curve_result.audit.reset_index(drop=True),
            data_quality=self.data.data_quality.reset_index(drop=True),
            run_config=_records_frame(config_rows, _RUN_CONFIG_COLUMNS),
            metrics=_summary_metrics(daily),
        )
