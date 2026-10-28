"""Fixed-quantity futures cash ledger around the existing price-return engine."""

import math

from common.minute.account import EventAccount

from .contracts import (
    DAILY_POLICY,
    TARGET_MISSING_REASONS,
    digest,
    number,
    require_keys,
    text,
    timestamp,
)
from .fees import MATCHING_VERSION, account_trades, account_trades_with_matching


def _mark(nav, positions, previous, prices, instruments, start, end):
    # Restore only the current event's economic exposure, not research history.
    # EventAccount's weights represent quantity * multiplier * previous price / NAV.
    # Re-create that exposure at every mark so contracts stay fixed as prices move.
    account = EventAccount(cost_bps=0.0)
    weights = {
        k: qty * instruments[k]["multiplier"] * previous[k] / nav
        for k, qty in positions.items()
    }
    account.initialize(previous)
    account.rebalance(
        timestamp(start), previous, weights, {k: "restore" for k in weights}
    )
    account.mark_close(timestamp(end).date(), timestamp(end), prices)
    return number(nav * (account.equity - 1), "mark PnL")


def _quotes(quote, kind, at, required, now, synthetic):
    keys = ["kind", "at", "available_at", "source", "synthetic", "prices"]
    if kind == "execution":
        keys.append("tradable")
    require_keys(quote, keys, "quote")
    if quote["kind"] != kind or timestamp(quote["at"]) != timestamp(at):
        raise ValueError("quote kind/timestamp mismatch")
    if quote["synthetic"] is not synthetic:
        raise ValueError("quote synthetic mismatch")
    if not timestamp(at) <= timestamp(quote["available_at"]) <= timestamp(now):
        raise ValueError("quote availability outside completed interval")
    text(quote["source"], "quote source")
    if not isinstance(quote["prices"], dict) or not required.issubset(quote["prices"]):
        raise ValueError("missing required contract price")
    if kind == "execution" and (
        not isinstance(quote["tradable"], dict)
        or any(quote["tradable"].get(k) is not True for k in required)
    ):
        raise ValueError("explicit tradable evidence required; no simulated fill")
    return {k: number(quote["prices"][k], "price", positive=True) for k in required}


def _daily_execution(quote, at, required, prior_prices, now, synthetic):
    """Execution quote under daily_policy: per-contract tradable evidence.

    Every required contract carries an explicit True/False. A False contract
    needs a stated reason and keeps its prior quantity; its 09:30 price is
    optional (limit-locked bars have one, missing bars do not). Returns the
    reference price used for marking and sizing, plus the untradable reasons.
    A held contract without a 09:30 price is referenced at its prior settle,
    so its whole-day PnL lands in the intraday mark and the total is unchanged.
    """
    require_keys(
        quote,
        (
            "kind",
            "at",
            "available_at",
            "source",
            "synthetic",
            "prices",
            "tradable",
            "untradable_reasons",
        ),
        "quote",
    )
    if quote["kind"] != "execution" or timestamp(quote["at"]) != timestamp(at):
        raise ValueError("quote kind/timestamp mismatch")
    if quote["synthetic"] is not synthetic:
        raise ValueError("quote synthetic mismatch")
    if not timestamp(at) <= timestamp(quote["available_at"]) <= timestamp(now):
        raise ValueError("quote availability outside completed interval")
    text(quote["source"], "quote source")
    tradable, reasons, prices = (
        quote["tradable"],
        quote["untradable_reasons"],
        quote["prices"],
    )
    if (
        not isinstance(tradable, dict)
        or not isinstance(reasons, dict)
        or not isinstance(prices, dict)
        or not required.issubset(tradable)
        or any(type(tradable[k]) is not bool for k in required)
    ):
        raise ValueError("explicit tradable evidence required; no simulated fill")
    blocked = {k for k in required if tradable[k] is False}
    if set(reasons) != blocked:
        raise ValueError("untradable reasons must match untradable contracts exactly")
    untradable = {k: text(reasons[k], "untradable reason") for k in sorted(blocked)}
    reference = {}
    for k in required:
        if k in prices:
            reference[k] = number(prices[k], "price", positive=True)
        elif k in blocked:
            if k in prior_prices:
                reference[k] = prior_prices[k]
        else:
            raise ValueError("missing required contract price")
    return reference, untradable


def _check_specs(instruments, required, trade_date):
    for k in required:
        if k not in instruments:
            raise ValueError("contract multiplier/margin specification missing")
        spec = instruments[k]
        if not spec["valid_from"] <= trade_date <= spec["valid_to"]:
            raise ValueError("contract specification outside validity")


def _rebase_matching(config, state, trade_date):
    """Rebind DCE fee matching to an append-extended fee schedule.

    Under daily_policy, reference extensions add whole new contracts to the
    fee schedule, which changes the schedule digest the matching state binds.
    Same-day openings are the only evidence that depends on the schedule, and
    a new trade date discards them anyway, so rebinding at a day boundary
    loses nothing. Within a day the digest check stays strict. Each opening
    lot still binds its own rule digest, so an edited rule still fails.
    """
    current = digest(config["fee_model"])
    if (
        config.get("daily_policy") is None
        or state is None
        or state.get("model_sha256") == current
        or state.get("trade_date") == trade_date
    ):
        return state, False
    return {**state, "model_sha256": current, "unmatched": {}}, True


def _target_missing(evidence):
    require_keys(evidence, ("reason", "detail", "evidence"), "target_missing")
    if evidence["reason"] not in TARGET_MISSING_REASONS:
        raise ValueError("unsupported target_missing reason")
    text(evidence["detail"], "target_missing detail")
    if not isinstance(evidence["evidence"], dict):
        raise ValueError("target_missing evidence object required")
    return evidence


def _validate_target(config, pinned, interval, target, available_at):
    require_keys(
        target,
        (
            "source_version",
            "available_at",
            "csv_sha256",
            "metadata_sha256",
            "row_count",
            "signal_date",
            "synthetic",
            "parameters",
            "weights",
        ),
        "target",
    )
    if (
        target["source_version"] != config["source_version"]
        or target["parameters"] != config["source_parameters"]
    ):
        raise ValueError("source version/parameters mismatch")
    if target["synthetic"] is not config["synthetic"]:
        raise ValueError("target synthetic mismatch")
    for key in ("csv_sha256", "metadata_sha256"):
        value = target[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("source file SHA-256 required")
    if pinned is None:
        if target["signal_date"] != interval["signal_date"]:
            raise ValueError("target signal_date is not the calendar's prior trading day")
        if (
            not timestamp(interval["start_at"])
            <= timestamp(target["available_at"])
            <= timestamp(interval["execution_at"])
        ):
            raise ValueError("target not available before simulated execution")
    else:
        declaration = pinned["pinned"]
        if (
            target["csv_sha256"] != declaration["csv_sha256"]
            or target["metadata_sha256"] != declaration["metadata_sha256"]
            or target["row_count"] != declaration["row_count"]
            or target["signal_date"] != declaration["signal_date"]
            or timestamp(target["available_at"]) != timestamp(declaration["available_at"])
        ):
            raise ValueError("target batch does not match the pinned declaration")
    if timestamp(interval["end_at"]) > timestamp(available_at):
        raise ValueError("full valuation interval has not completed")
    if (
        not isinstance(target["weights"], dict)
        or type(target["row_count"]) is not int
        or len(target["weights"]) != target["row_count"]
    ):
        raise ValueError("target row count mismatch")
    return {k: number(v, "target_weight") for k, v in target["weights"].items()}


def calculate(config, prior, interval, packet, *, available_at):
    pinned = config.get("target_policy")
    daily = config.get("daily_policy")
    if daily is not None and daily != DAILY_POLICY:
        raise ValueError("unsupported daily_policy")
    # pinned_batch: once the first build holds contracts, later intervals keep
    # the fixed quantities and never need an execution quote (settle-to-settle
    # marks only). An empty first build retries the build on later intervals.
    holding = pinned is not None and bool(prior["positions"])
    # daily_policy: no qualifying target by the execution time means no trade.
    # Positions carry and mark settle-to-settle; an older target never stands in.
    missing = (
        daily is not None and isinstance(packet, dict) and packet.get("target") is None
    )
    if missing:
        require_keys(
            packet,
            ("trade_date", "target", "target_missing", "settlement"),
            "interval input",
        )
    else:
        require_keys(
            packet,
            ("trade_date", "target", "settlement")
            + (() if holding else ("execution",)),
            "interval input",
        )
    if packet["trade_date"] != interval["trade_date"]:
        raise ValueError("next calendar trade_date required")
    target_missing = (
        _target_missing(packet["target_missing"]) if missing else None
    )
    if missing:
        target = None
        weights = {}
        holding = True
        if timestamp(interval["end_at"]) > timestamp(available_at):
            raise ValueError("full valuation interval has not completed")
    else:
        target = packet["target"]
        weights = _validate_target(config, pinned, interval, target, available_at)
    held = prior["positions"]
    if daily is None:
        required = set(held) | set(weights)
    else:
        # A zero-weight row for an unheld contract implies no trade; it needs
        # neither a specification nor a quote.
        required = set(held) | {k for k, w in weights.items() if w}
    instruments = config["instruments"]
    _check_specs(instruments, required, interval["trade_date"])
    opening_nav = prior["nav"]
    untradable = {}
    rebased = False
    if holding:
        # No trades are possible: quantities are the held integers, no fees
        # accrue, and the period PnL runs settlement-to-settlement below.
        execution = None
        gap_pnl = 0.0
        pretrade_nav = opening_nav
        quantities = {k: v for k, v in held.items() if v}
        position_lots = prior.get("position_lots")
        fee_matching_state = prior.get("fee_matching_state")
        trades = []
    else:
        if daily is None:
            execution = _quotes(
                packet["execution"],
                "execution",
                interval["execution_at"],
                required,
                available_at,
                config["synthetic"],
            )
        else:
            execution, untradable = _daily_execution(
                packet["execution"],
                interval["execution_at"],
                required,
                prior["prices"],
                available_at,
                config["synthetic"],
            )
        gap_pnl = _mark(
            opening_nav,
            held,
            prior["prices"],
            execution,
            instruments,
            prior["as_of"],
            interval["execution_at"],
        )
        pretrade_nav = number(opening_nav + gap_pnl, "pretrade NAV", positive=True)
        quantities = {}
        for k, weight in weights.items():
            if daily is not None and (not weight or k in untradable):
                continue
            q = weight * pretrade_nav / (execution[k] * instruments[k]["multiplier"])
            if config["lot_rule"] == "nearest_integer_ties_even":
                q = round(q)
            if q:
                quantities[k] = number(q, "quantity")
        for k in untradable:
            # No simulated fill: an untradable contract keeps yesterday's lots.
            if held.get(k):
                quantities[k] = held[k]
        position_lots = None
        fee_matching_state = None
        if "fee_model" in config:
            position_lots = prior.get("position_lots")
            if position_lots is not None:
                for rows in position_lots.values():
                    if any(
                        timestamp(lot["opened_at"]) > timestamp(prior["as_of"])
                        for lot in rows
                    ):
                        raise ValueError("opening evidence later than prior account state")
            fee_args = (
                config["fee_model"],
                held,
                position_lots,
                quantities,
                execution,
                instruments,
                interval["trade_date"],
                interval["execution_at"],
                packet["execution"]["source"],
            )
            if config["fee_model"]["version"] == MATCHING_VERSION:
                fee_matching_state = prior.get("fee_matching_state")
                fee_matching_state, rebased = _rebase_matching(
                    config, fee_matching_state, interval["trade_date"]
                )
                if fee_matching_state and fee_matching_state["last_at"] is not None:
                    if timestamp(fee_matching_state["last_at"]) > timestamp(
                        prior["as_of"]
                    ):
                        raise ValueError(
                            "fee matching evidence later than prior account state"
                        )
                (
                    trades,
                    position_lots,
                    fee_matching_state,
                ) = account_trades_with_matching(
                    *fee_args, matching_state=fee_matching_state
                )
            else:
                trades, position_lots = account_trades(*fee_args)
        else:
            trades = []
            for k in sorted(required):
                delta = quantities.get(k, 0.0) - held.get(k, 0.0)
                if delta:
                    notional = abs(delta) * execution[k] * instruments[k]["multiplier"]
                    trades.append(
                        {
                            "contract": k,
                            "quantity_change": delta,
                            "price": execution[k],
                            "notional": notional,
                            "fee": notional * config["cost_bps"] / 10000,
                        }
                    )
    fees = math.fsum(t["fee"] for t in trades)
    nav_after_fee = number(pretrade_nav - fees, "NAV after fees", positive=True)
    execution_margin = (
        None
        if execution is None
        else math.fsum(
            abs(q)
            * execution[k]
            * instruments[k]["multiplier"]
            * instruments[k]["margin_rate"]
            for k, q in quantities.items()
        )
    )
    if execution_margin is not None and execution_margin > nav_after_fee:
        raise ValueError("insufficient margin at execution; borrowing unsupported")
    settlement = _quotes(
        packet["settlement"],
        "settlement",
        interval["end_at"],
        set(quantities),
        available_at,
        config["synthetic"],
    )
    intraday_pnl = _mark(
        nav_after_fee,
        quantities,
        prior["prices"] if holding else execution,
        settlement,
        instruments,
        prior["as_of"] if holding else interval["execution_at"],
        interval["end_at"],
    )
    closing_nav = number(nav_after_fee + intraday_pnl, "closing NAV", positive=True)
    gross = math.fsum(
        abs(q) * settlement[k] * instruments[k]["multiplier"]
        for k, q in quantities.items()
    )
    margin = math.fsum(
        abs(q)
        * settlement[k]
        * instruments[k]["multiplier"]
        * instruments[k]["margin_rate"]
        for k, q in quantities.items()
    )
    if margin > closing_nav:
        raise ValueError(
            "insufficient margin at settlement; liquidation model unsupported"
        )
    # Daily variation margin settles all marked PnL into cash. Contract notional
    # is never debited like a stock purchase. Margin is reserved, not an expense.
    unit_nav = config["initial_unit_nav"] * closing_nav / config["initial_capital"]
    row = {
        "trade_date": interval["trade_date"],
        "period_start_at": interval["start_at"],
        "period_end_at": interval["end_at"],
        "available_at": available_at,
        "daily_return": closing_nav / opening_nav - 1,
        "unit_nav": unit_nav,
        "gross_ratio": gross / closing_nav,
        "margin_ratio": margin / closing_nav,
    }
    return {
        **({"position_lots": position_lots} if position_lots is not None else {}),
        **(
            {"fee_matching_state": fee_matching_state}
            if fee_matching_state is not None
            else {}
        ),
        "initialized_at": prior["initialized_at"],
        "as_of": interval["end_at"],
        "next_index": prior["next_index"] + 1,
        "positions": quantities,
        "prices": settlement,
        "nav": closing_nav,
        "cash": closing_nav,
        "margin": margin,
        "free_cash": closing_nav - margin,
        "pnl": gap_pnl + intraday_pnl,
        "fees": fees,
        "row": row,
        "audit": {
            "fee_model_version": config.get("fee_model", {}).get(
                "version", "uniform-bps-v1"
            ),
            "signal_date": None if target is None else target["signal_date"],
            "execution_at": interval["execution_at"],
            "settlement_at": interval["end_at"],
            "pretrade_nav": pretrade_nav,
            "gap_pnl": gap_pnl,
            "intraday_pnl": intraday_pnl,
            "trades": trades,
            "target_weights": weights,
            "target_policy": None if pinned is None else pinned["mode"],
            "execution_margin": execution_margin,
            "settlement_rule": config["valuation_rule"],
            **(
                {}
                if daily is None
                else {
                    "target_status": "missing" if missing else "valid",
                    "target_missing": target_missing,
                    "untradable": untradable,
                    "fee_matching_rebased": rebased,
                }
            ),
        },
    }
