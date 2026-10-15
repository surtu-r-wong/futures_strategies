"""Versioned explicit contract fees and FIFO dated net positions.

A same-day close can adjust its opening fee only under the explicitly sourced
ordinary-open-then-adjust policy. This module does not infer exchange policy.
"""

from copy import deepcopy
import math

from .contracts import day, digest, number, numeric_field, require_keys, text, timestamp

VERSION = "contract-fees-v1"
ACTIONS = ("open", "close", "close_today", "intraday_open")
UNITS = {"YUAN_PER_LOT", "PERMILLE", "PER_TEN_THOUSAND"}


def _rate(rate):
    require_keys(rate, ("value", "unit"), "fee rate")
    if rate["unit"] not in UNITS:
        raise ValueError("unknown fee unit; explicit conversion required")
    numeric_field(rate, "value", nonnegative=True)


def charge(rate, quantity, price, multiplier):
    """CNY charge; percentage units never inferred from rate magnitude."""
    _rate(rate)
    q = abs(number(quantity, "quantity"))
    price = number(price, "price", positive=True)
    multiplier = number(multiplier, "multiplier", positive=True)
    amount = q * rate["value"]
    if rate["unit"] != "YUAN_PER_LOT":
        divisor = 1000 if rate["unit"] == "PERMILLE" else 10000
        amount *= price * multiplier / divisor
    return number(amount, "fee", nonnegative=True)


def validate_model(model, instruments):
    require_keys(
        model,
        (
            "version",
            "position_accounting",
            "close_order",
            "broker_fee_basis",
            "intraday_open_policy",
            "policy_source",
            "excluded_costs",
            "rules",
        ),
        "fee model",
    )
    for key, value in {
        "version": VERSION,
        "position_accounting": "dated-net-lots-v1",
        "close_order": "fifo",
        "broker_fee_basis": "additional_to_exchange",
        "intraday_open_policy": "ordinary_open_then_same_day_adjustment",
    }.items():
        if model[key] != value:
            raise ValueError(f"unsupported fee model {key}")
    text(model["policy_source"], "intraday opening policy source")
    if not isinstance(model["excluded_costs"], list):
        raise ValueError("explicit excluded_costs list required")
    for item in model["excluded_costs"]:
        text(item, "excluded cost")
    rules = model["rules"]
    if not isinstance(rules, dict) or set(rules) != set(instruments):
        raise ValueError("fee rules must cover exactly the specified contracts")
    for contract, schedule in rules.items():
        text(contract, "fee contract")
        if not isinstance(schedule, list) or not schedule:
            raise ValueError("dated fee schedule required")
        ids, periods = set(), []
        for rule in schedule:
            require_keys(
                rule,
                (
                    "rule_id",
                    "valid_from",
                    "valid_to",
                    "exchange_source",
                    "broker_source",
                    "slippage_source",
                    "exchange",
                    "broker",
                    "slippage",
                ),
                "fee rule",
            )
            for key in (
                "rule_id",
                "exchange_source",
                "broker_source",
                "slippage_source",
            ):
                text(rule[key], key)
            start, end = day(rule["valid_from"]), day(rule["valid_to"])
            if start > end or rule["rule_id"] in ids:
                raise ValueError("invalid fee validity or duplicate rule id")
            ids.add(rule["rule_id"])
            periods.append((start, end))
            for component in ("exchange", "broker"):
                require_keys(rule[component], ACTIONS, component)
                for rate in rule[component].values():
                    _rate(rate)
            _rate(rule["slippage"])
        periods.sort()
        if any(b[0] <= a[1] for a, b in zip(periods, periods[1:])):
            raise ValueError("overlapping fee validity periods")


def _rule(model, contract, trade_date):
    matches = [
        r
        for r in model["rules"].get(contract, [])
        if r["valid_from"] <= trade_date <= r["valid_to"]
    ]
    if len(matches) != 1:
        raise ValueError(f"fee rule outside validity: {contract} / {trade_date}")
    return matches[0]


def _validate_lots(model, positions, lots, instruments, trade_date, at):
    if lots is None:
        raise ValueError("dated position evidence missing; explicit migration required")
    if not isinstance(lots, dict) or set(lots) != set(positions):
        raise ValueError("position lots do not reconcile to held contracts")
    ids = set()
    for contract, rows in lots.items():
        if not isinstance(rows, list) or not rows:
            raise ValueError("nonempty position lots required")
        held = number(positions[contract], "held quantity")
        if held == 0:
            raise ValueError("zero held position must not have lots")
        quantities = []
        for lot in rows:
            require_keys(
                lot,
                (
                    "lot_id",
                    "quantity",
                    "opened_trade_date",
                    "opened_at",
                    "entry_price",
                    "multiplier",
                    "opening_rule_sha256",
                    "source",
                ),
                "position lot",
            )
            text(lot["lot_id"], "lot id")
            text(lot["source"], "opening evidence source")
            if lot["lot_id"] in ids:
                raise ValueError("duplicate position lot id")
            ids.add(lot["lot_id"])
            qty = numeric_field(lot, "quantity")
            if qty * held <= 0:
                raise ValueError("position lots must have the net position direction")
            opened = day(lot["opened_trade_date"])
            opened_at = timestamp(lot["opened_at"])
            if (
                opened > day(trade_date)
                or opened_at > timestamp(at)
                or opened_at.date() != opened
            ):
                raise ValueError("invalid opening date evidence for day-session model")
            numeric_field(lot, "entry_price", positive=True)
            if lot["multiplier"] != instruments[contract]["multiplier"]:
                raise ValueError("lot multiplier changed; explicit migration required")
            original = _rule(model, contract, lot["opened_trade_date"])
            if lot["opening_rule_sha256"] != digest(original):
                raise ValueError(
                    "opening fee evidence changed; explicit migration required"
                )
            quantities.append(qty)
        if not math.isclose(math.fsum(quantities), held, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("position lots do not reconcile to held quantity")


def account_trades(
    model, positions, lots, quantities, prices, instruments, trade_date, at, source
):
    """Return (split fills, new lots), without mutating inputs or using a clock.

    A net reversal closes dated lots first, then opens the opposite side.
    Fees/slippage are separate CNY costs; signed opening adjustments can refund.
    The calling account books their sum once and then checks margin after costs.
    """
    validate_model(model, instruments)
    day(trade_date)
    if timestamp(at).date() != day(trade_date):
        raise ValueError("day-session trade date must match execution timestamp")
    text(source, "execution source")
    _validate_lots(model, positions, lots, instruments, trade_date, at)
    result = deepcopy(lots)
    trades = []
    required = set(positions) | set(quantities) | set(prices)
    for contract in sorted(required):
        rule = _rule(model, contract, trade_date)
        price = number(prices[contract], "execution price", positive=True)
        mult = number(instruments[contract]["multiplier"], "multiplier", positive=True)
        old = number(positions.get(contract, 0), "old quantity")
        new = number(quantities.get(contract, 0), "new quantity")
        close_qty = abs(old) if old * new <= 0 else max(0, abs(old) - abs(new))

        def fill(action, delta, lot=None):
            q = abs(delta)
            exchange = charge(rule["exchange"][action], q, price, mult)
            broker = charge(rule["broker"][action], q, price, mult)
            slippage = charge(rule["slippage"], q, price, mult)
            adjustments = dict.fromkeys(("exchange", "broker"), 0.0)
            if action == "close_today":
                opening = _rule(model, contract, lot["opened_trade_date"])
                for component in adjustments:
                    adjustments[component] = charge(
                        opening[component]["intraday_open"], q, lot["entry_price"], mult
                    ) - charge(opening[component]["open"], q, lot["entry_price"], mult)
            trades.append(
                {
                    "contract": contract,
                    "action": action,
                    "quantity_change": delta,
                    "position_side": "long"
                    if (lot["quantity"] if lot else delta) > 0
                    else "short",
                    "price": price,
                    "notional": q * price * mult,
                    "exchange_fee": exchange,
                    "broker_fee": broker,
                    "slippage_fee": slippage,
                    "exchange_open_adjustment": adjustments["exchange"],
                    "broker_open_adjustment": adjustments["broker"],
                    "fee": number(
                        math.fsum([exchange, broker, slippage, *adjustments.values()]),
                        "total fee",
                    ),
                    "rule_id": rule["rule_id"],
                    "rule_sha256": digest(rule),
                    "closed_lot_id": lot["lot_id"] if lot else None,
                    "opened_trade_date": lot["opened_trade_date"]
                    if lot
                    else trade_date,
                }
            )

        remaining = []
        for lot in sorted(
            result.get(contract, []),
            key=lambda x: (timestamp(x["opened_at"]), x["lot_id"]),
        ):
            taken = min(close_qty, abs(lot["quantity"]))
            if taken:
                action = (
                    "close_today" if lot["opened_trade_date"] == trade_date else "close"
                )
                sign = 1 if old > 0 else -1
                fill(action, -sign * taken, lot)
                lot["quantity"] -= sign * taken
                close_qty -= taken
            if lot["quantity"]:
                remaining.append(lot)
        if close_qty > 1e-12:
            raise ValueError("position lots cannot cover closing quantity")
        open_qty = abs(new) if old * new <= 0 else max(0, abs(new) - abs(old))
        if open_qty:
            delta = open_qty if new > 0 else -open_qty
            fill("open", delta)
            remaining.append(
                {
                    "lot_id": digest([contract, trade_date, at, delta, lots, source]),
                    "quantity": delta,
                    "opened_trade_date": trade_date,
                    "opened_at": at,
                    "entry_price": price,
                    "multiplier": mult,
                    "opening_rule_sha256": digest(rule),
                    "source": source,
                }
            )
        if remaining:
            result[contract] = remaining
        else:
            result.pop(contract, None)
    return trades, result


def description(config):
    if "fee_model" not in config:
        return f"uniform-bps-v1; {config['cost_bps']} bps per traded notional"
    model = config["fee_model"]
    return (
        f"{VERSION}; schedule_sha256={digest(model)}; "
        "exchange + additional broker + explicit slippage; "
        f"excluded_costs={model['excluded_costs']}"
    )
