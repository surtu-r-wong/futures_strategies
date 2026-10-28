"""M02 v2 nightly: follow the 027 strategy one trading day at a time.

Debian crontab 21:05 (retry 21:35 via m02_v2_nightly.sh). For every due
calendar interval T, in order:

  seal       a sealed store never advances (SEALED verdict, zero write)
  target     <targets-root>/<T>/ must be a `ready` package whose signal date is
             the calendar's prior trading day and whose available_at falls in
             [start_at, 09:30]; anything else is a mechanical target_missing
             (no trade, positions carry, settle-to-settle mark), never a failure
             and never an older target
  minute     every exchange the day touches (held + nonzero target) must have
             day-session rows in public.futures_minute; an exchange with zero
             rows is "not collected yet" -> FAIL closed, retried on later nights
  execution  09:30:00 bar open per contract; no bar -> untradable `no_0930_bar`,
             open at/through the day's exchange limit price ->
             `limit_locked_at_open`; an untradable contract keeps its lots
  specs      contracts new to the account are derived from the signal day's
             exchange parameters (forward_inputs.m02_v2_specs); any that cannot
             be derived -> FAIL closed, nothing dropped
  settle     public.futures_daily settle for every contract the day ends with
  dry run    the whole interval (reference extension + advance) on a scratch
             copy of the store; its report is kept as evidence
  commit     the same on the real store, then feed + daily status + verdict,
             with the money-chain self-check

--dry-run-only stops after the dry run (zero writes to the store). Exit
codes: 0 PASS/SKIP/SEALED, 2 FAIL.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forward_account.adapters import read_targets  # noqa: E402
from forward_account.contracts import now_iso, timestamp  # noqa: E402
from forward_account.store import (  # noqa: E402
    _account,
    _references,
    advance,
    connection,
    export_feed,
    extend_references,
    read_status,
)
from forward_inputs import m02_v2_specs as specs  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TZ = dt.timezone(dt.timedelta(hours=8))
VERDICT_SCHEMA = "m02-v2-nightly-verdict.v1"
STATUS_SCHEMA = "m02-v2-daily-status.v1"
MAX_INTERVALS_DEFAULT = 15
CALENDAR_RUNWAY_ALERT = 15
MINUTE_EXCHANGE = {"SHFE": "SHFE", "INE": "INE", "DCE": "DCE", "CZCE": "CZCE", "GFEX": "GFEX"}


class GateFailure(Exception):
    def __init__(self, stage, error, **extra):
        super().__init__(error)
        self.stage, self.error, self.extra = stage, error, extra


# --------------------------------------------------------------------- seal


def seal_path(store):
    return Path(str(store) + ".sealed.json")


def is_sealed(store):
    return seal_path(store).is_file()


# ------------------------------------------------------------------- target


def classify_target(targets_root, interval, config):
    """(target, None) for a qualifying package, else (None, target_missing)."""
    trade_date = interval["trade_date"]
    root = Path(targets_root) / trade_date
    evidence = {"package": str(root)}

    def missing(reason, detail, **more):
        return None, {"reason": reason, "detail": detail, "evidence": {**evidence, **more}}

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return missing("not_produced", "no target package for this trade date")
    raw = manifest_path.read_bytes()
    evidence["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        manifest = json.loads(raw)
    except ValueError:
        return missing("validation_failed", "manifest is not valid JSON")
    evidence["manifest_status"] = manifest.get("status")
    if manifest.get("status") != "ready":
        return missing(
            "not_produced",
            f"package status {manifest.get('status')!r}: "
            f"{manifest.get('reason')} {manifest.get('detail') or ''}".strip(),
        )
    receipt = manifest.get("source_receipt") or {}
    evidence.update(
        signal_date=receipt.get("signal_date"),
        available_at=receipt.get("available_at"),
        csv_sha256=receipt.get("csv_sha256"),
    )
    if receipt.get("signal_date") != interval["signal_date"]:
        return missing(
            "signal_date_mismatch",
            f"signal_date {receipt.get('signal_date')} != calendar {interval['signal_date']}",
        )
    try:
        available = timestamp(receipt.get("available_at"))
    except ValueError:
        return missing("validation_failed", "available_at is not an aware timestamp")
    if not timestamp(interval["start_at"]) <= available <= timestamp(interval["execution_at"]):
        return missing(
            "not_available_before_execution",
            f"available_at {receipt.get('available_at')} outside "
            f"[{interval['start_at']}, {interval['execution_at']}]",
        )
    try:
        target = read_targets(
            config, root / "targets.csv", root / "source-parameters.json", receipt
        )
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return missing("validation_failed", f"{type(exc).__name__}: {exc}")
    return target, None


# ---------------------------------------------------------------- execution


def minute_symbol(contract):
    exchange, _, code = specs.split_code(contract)
    return code, MINUTE_EXCHANGE[exchange]


def execution_evidence(needed, bars, limits):
    """prices / tradable / reasons for every needed contract.

    bars: {contract: open at the 09:30:00 bar}; limits: {contract: (up, down)}
    for the trade date. A bar without a limit row is a FAIL (the day cannot be
    judged), never a guess.
    """
    prices, tradable, reasons, no_limits = {}, {}, {}, []
    for contract in sorted(needed):
        if contract not in bars:
            tradable[contract] = False
            reasons[contract] = "no_0930_bar"
            continue
        price = bars[contract]
        prices[contract] = price
        if contract not in limits:
            no_limits.append(contract)
            continue
        up, down = limits[contract]
        if price >= up or price <= down:
            tradable[contract] = False
            reasons[contract] = "limit_locked_at_open"
        else:
            tradable[contract] = True
    if no_limits:
        raise GateFailure("limit_gate", f"no exchange limit prices for: {', '.join(no_limits)}")
    return prices, tradable, reasons


# ----------------------------------------------------------------- sources


class PgSources:
    """Read-only lookups for one trade date (REPEATABLE READ snapshot)."""

    def __init__(self, source):
        self.source = source

    def _rows(self, sql, params):
        return self.source.query(sql, params).to_dict("records")

    def minute_counts(self, trade_date):
        rows = self._rows(
            "SELECT exchange, count(*) AS n FROM public.futures_minute "
            "WHERE bar_time >= %(lo)s AND bar_time < %(hi)s GROUP BY exchange",
            {"lo": f"{trade_date}T08:30:00+08:00", "hi": f"{trade_date}T15:30:00+08:00"},
        )
        return {r["exchange"]: int(r["n"]) for r in rows}

    def bars(self, trade_date, contracts):
        wanted = {minute_symbol(c): c for c in contracts}
        rows = self._rows(
            "SELECT symbol, exchange, open FROM public.futures_minute "
            "WHERE bar_time = %(at)s AND symbol = ANY(%(symbols)s)",
            {"at": f"{trade_date}T09:30:00+08:00", "symbols": sorted({s for s, _ in wanted})},
        )
        out = {}
        for r in rows:
            contract = wanted.get((r["symbol"], r["exchange"]))
            if contract and r["open"] is not None and float(r["open"]) > 0:
                out[contract] = float(r["open"])
        return out

    def limits(self, trade_date, contracts):
        rows = specs.fetch(self.source.query, specs.INFO_SQL, trade_date)
        index = {(r["exchange"], r["contract_id"]): r for r in rows}
        out = {}
        for c in contracts:
            exchange, _, _ = specs.split_code(c)
            r = index.get((exchange, specs.source_id(c)))
            if r and r["limit_up"] and r["limit_down"]:
                out[c] = (float(r["limit_up"]), float(r["limit_down"]))
        return out

    def settles(self, trade_date, contracts):
        rows = self._rows(
            "SELECT symbol, settle FROM public.futures_daily "
            "WHERE trade_date = %(day)s AND symbol = ANY(%(symbols)s)",
            {"day": trade_date, "symbols": sorted(contracts)},
        )
        return {
            r["symbol"]: float(r["settle"])
            for r in rows
            if r["settle"] is not None and float(r["settle"]) > 0
        }

    def spec_rows(self, signal_date):
        return (
            specs.fetch(self.source.query, specs.SETTLE_SQL, signal_date),
            specs.fetch(self.source.query, specs.INFO_SQL, signal_date),
        )


# ----------------------------------------------------------------- interval


def current_references(store, account):
    with connection(store, readonly=True) as con:
        config, calendar, revision = _references(con, _account(con, account))
    return config, calendar, revision


def new_specs(config, needed, trade_date, signal_date, sources, table):
    """Derive specs for contracts the account has not registered yet."""
    fresh = sorted(set(needed) - set(config["instruments"]))
    if not fresh:
        return {}, {}
    settle_rows, info_rows = sources.spec_rows(signal_date)
    settle = {(r["exchange"], r["contract_id"]): r for r in settle_rows}
    info = {(r["exchange"], r["contract_id"]): r for r in info_rows}
    instruments, rules, failures = {}, {}, {}
    for contract in fresh:
        try:
            exchange, _, _ = specs.split_code(contract)
            key = (exchange, specs.source_id(contract))
            instruments[contract], rules[contract] = specs.derive(
                contract, trade_date, table, settle.get(key), info.get(key)
            )
        except specs.SpecMissing as exc:
            failures[contract] = str(exc)
    if failures:
        raise GateFailure("spec_gate", "specification missing", spec_missing=failures)
    return instruments, rules


def prepare(store, account, interval, targets_root, sources, table):
    """Gate one interval and assemble its packet + reference additions."""
    config, calendar, _ = current_references(store, account)
    state = read_status(store, account)["state"]
    held = {k for k, v in state["positions"].items() if v}
    trade_date, signal_date = interval["trade_date"], interval["signal_date"]
    target, missing = classify_target(targets_root, interval, config)
    nonzero = set() if target is None else {k for k, w in target["weights"].items() if w}
    needed = held | nonzero
    exchanges = {specs.split_code(c)[0] for c in needed}
    # Minute bars matter only when the day executes; a missing target holds.
    if target is not None and needed:
        counts = sources.minute_counts(trade_date)
        empty = sorted(e for e in exchanges if not counts.get(MINUTE_EXCHANGE[e]))
        if empty:
            raise GateFailure(
                "minute_gate",
                f"public.futures_minute has zero day-session rows for {', '.join(empty)} "
                f"on {trade_date} (collection not run yet); retry on a later night",
                minute_counts=counts,
            )
    instruments, rules = new_specs(config, nonzero - held, trade_date, signal_date, sources, table)
    packet = {"trade_date": trade_date}
    if target is None:
        packet.update(target=None, target_missing=missing)
        settle_needed = held
        untradable = {}
    else:
        bars = sources.bars(trade_date, needed)
        limits = sources.limits(trade_date, set(bars))
        prices, tradable, untradable = execution_evidence(needed, bars, limits)
        packet["target"] = target
        packet["execution"] = {
            "kind": "execution",
            "at": interval["execution_at"],
            "available_at": now_iso(),
            "source": (
                "public.futures_minute 09:30:00 bar open (five exchanges, one source); "
                "tradable = bar present and open strictly inside the trade date's "
                "public.futures_contract_info limit prices; slippage charged separately "
                "as 4bp CNY cost"
            ),
            "synthetic": config["synthetic"],
            "prices": prices,
            "tradable": tradable,
            "untradable_reasons": untradable,
        }
        settle_needed = held | {k for k in nonzero if tradable.get(k)}
    settles = sources.settles(trade_date, needed)
    absent = sorted(settle_needed - set(settles))
    if absent:
        raise GateFailure(
            "settle_gate", f"futures_daily settle missing for: {', '.join(absent)}"
        )
    packet["settlement"] = {
        "kind": "settlement",
        "at": interval["end_at"],
        "available_at": now_iso(),
        "source": "public.futures_daily settle (EOD); read-only query",
        "synthetic": config["synthetic"],
        "prices": {k: settles[k] for k in sorted(settles)},
    }
    return packet, instruments, rules, calendar, config, untradable


def apply(store, account, packet, instruments, rules, calendar, config):
    if instruments:
        extend_references(
            store,
            account,
            calendar,
            {**config["instruments"], **instruments},
            {**config["fee_model"]["rules"], **rules},
        )
    return advance(store, account, packet)


def dry_run(store, account, packet, instruments, rules, calendar, config):
    """The identical path on a scratch copy; the real store is untouched."""
    with tempfile.TemporaryDirectory(prefix="m02v2-dry-") as tmp:
        copy = Path(tmp) / "account.sqlite"
        shutil.copy2(store, copy)
        return apply(copy, account, packet, instruments, rules, calendar, config)


def report(packet, state, instruments):
    target = packet.get("target")
    execution = packet.get("execution") or {}
    return {
        "trade_date": packet["trade_date"],
        "target_status": "missing" if target is None else "valid",
        "target_missing": packet.get("target_missing"),
        "target_csv_sha256": None if target is None else target["csv_sha256"],
        "target_available_at": None if target is None else target["available_at"],
        "signal_date": None if target is None else target["signal_date"],
        "execution_prices": execution.get("prices", {}),
        "untradable": execution.get("untradable_reasons", {}),
        "new_contracts": sorted(instruments),
        "trades": [
            {k: t[k] for k in ("contract", "action", "quantity_change", "price", "fee")}
            for t in state["audit"]["trades"]
        ],
        "positions": state["positions"],
        "fees": state["fees"],
        "pnl": state["pnl"],
        "nav": state["nav"],
        "unit_nav": state["row"]["unit_nav"],
        "daily_return": state["row"]["daily_return"],
        "margin_ratio": state["row"]["margin_ratio"],
        "gross_ratio": state["row"]["gross_ratio"],
    }


def expiry_alerts(store, account, state, nxt):
    """Held contracts that will have no settle on the next interval.

    Registered specs end on the contract's last trading day; a position still
    open past it means the native roll did not happen (e.g. untradable on the
    last day). The next night will fail closed on the missing settle -- this
    says why one night early.
    """
    if nxt is None:
        return []
    config, _, _ = current_references(store, account)
    return [
        {
            "contract": k,
            "last_trade_date": config["instruments"][k]["valid_to"],
            "next_trade_date": nxt["trade_date"],
            "alert": "held past its last trading day; next interval will fail closed",
        }
        for k, q in sorted(state["positions"].items())
        if q and config["instruments"][k]["valid_to"] < nxt["trade_date"]
    ]


def money_chain(prev_nav, row, nav):
    residual = row["daily_return"] * prev_nav - (nav - prev_nav)
    return abs(residual) <= max(1e-6, abs(nav) * 1e-12), residual


# ------------------------------------------------------------ daily status


def export_daily_status(store, account, path):
    """Per-interval target/tradability markers for the consumer.

    The feed rows are a strict schema on the consumer side, so these markers
    travel in a sidecar next to the feed rather than inside it.
    """
    with connection(store, readonly=True) as con:
        rows = con.execute(
            """SELECT l.period_index, l.state_json FROM ledger l WHERE l.account_id=?
            AND l.revision=(SELECT MAX(x.revision) FROM ledger x
                WHERE x.account_id=l.account_id AND x.period_index=l.period_index)
            ORDER BY l.period_index""",
            (account,),
        ).fetchall()
    days = []
    for _, raw in rows:
        state = json.loads(raw)
        audit = state["audit"]
        days.append(
            {
                "trade_date": state["row"]["trade_date"],
                "target_status": audit.get("target_status"),
                "target_missing": audit.get("target_missing"),
                "signal_date": audit.get("signal_date"),
                "untradable": audit.get("untradable", {}),
                "trades": len(audit["trades"]),
                "positions": len(state["positions"]),
            }
        )
    payload = {"schema": STATUS_SCHEMA, "account_id": account, "days": days}
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    return payload


# ------------------------------------------------------------------ driver


def _git_commit():
    try:
        return subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _finish(verdict, verdict_dir, today):
    verdict["finished_at"] = dt.datetime.now(TZ).isoformat()
    payload = json.dumps(verdict, ensure_ascii=False, indent=1, default=str) + "\n"
    _atomic_write(Path(verdict_dir) / f"{today}.json", payload)
    _atomic_write(Path(verdict_dir) / "latest.json", payload)
    print(json.dumps({k: verdict.get(k) for k in ("status", "stage", "error", "today")}, ensure_ascii=False))
    return 0 if verdict["status"] in ("PASS", "SKIP", "SEALED") else 2


def run(args, sources_factory):
    today = args.today or dt.datetime.now(TZ).date().isoformat()
    verdict_dir = args.verdict_dir or str(Path(args.state).parent / "verdicts")
    verdict = {
        "schema": VERDICT_SCHEMA,
        "account_id": args.account,
        "mode": "dry_run_only" if args.dry_run_only else "commit",
        "started_at": dt.datetime.now(TZ).isoformat(),
        "host": socket.gethostname(),
        "commit": _git_commit(),
        "today": today,
        "rows": [],
        "dry_runs": [],
        "alerts": [],
    }
    try:
        if is_sealed(args.state):
            verdict.update(status="SEALED", seal=json.loads(seal_path(args.state).read_text()))
            return _finish(verdict, verdict_dir, today)
        table = specs.load_table(args.spec_table)
        verdict["spec_table_sha256"] = table["table_sha256"]
        status = read_status(args.state, args.account)
        verdict["engine_sha256"] = status["engine_sha256"]
        nxt = status["next_interval"]
        if nxt is None:
            verdict.update(status="FAIL", stage="calendar", error="calendar exhausted; extend references first")
            return _finish(verdict, verdict_dir, today)
        if nxt["trade_date"] > today:
            verdict.update(status="SKIP", next_interval=nxt, note="nothing due tonight")
            return _finish(verdict, verdict_dir, today)
        with sources_factory() as sources:
            while nxt is not None and nxt["trade_date"] <= today:
                if len(verdict["rows"]) >= args.max_intervals:
                    raise GateFailure("cap", f"interval cap {args.max_intervals} reached")
                prev_nav = status["state"]["nav"]
                try:
                    prepared = prepare(
                        args.state, args.account, nxt, args.targets_root, sources, table
                    )
                except GateFailure as exc:
                    exc.extra.setdefault("trade_date", nxt["trade_date"])
                    raise
                packet, instruments, rules, calendar, config, _ = prepared
                verdict["alerts"].extend(_precheck_alerts(args.targets_root, nxt))
                trial = dry_run(args.state, args.account, *prepared[:5])
                verdict["dry_runs"].append(report(packet, trial, instruments))
                if args.dry_run_only:
                    break
                state = apply(args.state, args.account, *prepared[:5])
                ok, residual = money_chain(prev_nav, state["row"], state["nav"])
                verdict["rows"].append({**report(packet, state, instruments), "pnl_residual": residual})
                if state["nav"] != trial["nav"]:
                    raise GateFailure("dry_run_mismatch", "committed NAV differs from the dry run")
                if not ok:
                    raise GateFailure("money_chain", f"residual {residual:.6f} CNY")
                status = read_status(args.state, args.account)
                nxt = status["next_interval"]
                verdict["alerts"].extend(
                    expiry_alerts(args.state, args.account, state, nxt)
                )
        if verdict["rows"] and args.feed:
            synthetic = current_references(args.state, args.account)[0]["synthetic"]
            export_feed(args.state, args.feed, synthetic=synthetic)
            export_daily_status(
                args.state, args.account, Path(args.feed).with_name("daily-status.json")
            )
        runway = _calendar_runway(args.state, args.account, today)
        if runway < CALENDAR_RUNWAY_ALERT:
            verdict["alerts"].append(f"calendar has {runway} intervals left; extend references")
        verdict["status"] = "PASS"
        return _finish(verdict, verdict_dir, today)
    except GateFailure as exc:
        verdict.update(status="FAIL", stage=exc.stage, error=exc.error, **exc.extra)
        return _finish(verdict, verdict_dir, today)
    except Exception as exc:  # verdict always lands
        verdict.update(status="FAIL", stage="exception", error=f"{type(exc).__name__}: {exc}")
        return _finish(verdict, verdict_dir, today)


def _precheck_alerts(targets_root, interval):
    path = Path(targets_root) / interval["trade_date"] / "precheck.json"
    if not path.is_file():
        return []
    check = json.loads(path.read_text())
    alerts = [
        {"trade_date": interval["trade_date"], **a} for a in check.get("expiry_alerts", [])
    ]
    if check.get("spec_missing"):
        alerts.append({"trade_date": interval["trade_date"], "spec_missing": check["spec_missing"]})
    if check.get("precheck_error"):
        alerts.append({"trade_date": interval["trade_date"], "precheck_error": check["precheck_error"]})
    return alerts


def _calendar_runway(store, account, today):
    _, calendar, _ = current_references(store, account)
    return sum(1 for i in calendar["intervals"] if i["trade_date"] > today)


class _PgFactory:
    def __enter__(self):
        from forward_inputs.m01 import ReadOnlySource

        self._source = ReadOnlySource()
        return PgSources(self._source.__enter__())

    def __exit__(self, *exc):
        return self._source.__exit__(*exc)


def main(argv=None, sources_factory=_PgFactory):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--targets-root", required=True)
    parser.add_argument("--feed", help="feed path; omit for no export (shadow)")
    parser.add_argument("--verdict-dir")
    parser.add_argument("--spec-table", default=str(specs.TABLE_PATH))
    parser.add_argument("--today", help="override the date gate (testing)")
    parser.add_argument("--max-intervals", type=int, default=MAX_INTERVALS_DEFAULT)
    parser.add_argument("--dry-run-only", action="store_true")
    return run(parser.parse_args(argv), sources_factory)


if __name__ == "__main__":
    raise SystemExit(main())
