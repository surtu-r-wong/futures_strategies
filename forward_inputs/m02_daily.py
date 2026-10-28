"""M02 v2 morning target package: one native 027 target per trading day.

    .venv/bin/python -m forward_inputs.m02_daily --trade-date 2026-11-16 \\
        --calendar <v2 calendar.json> --output-root <state>/targets \\
        [--store <account.sqlite> --account <id>]

Runs before the 09:30 execution of `trade_date` (Debian cron 08:30). The
signal date is the calendar's prior trading day S; the target is computed
only when all five commodity exchanges have S in public.futures_daily, with
PRODUCTION parameters and the native 900-day window ending at S -- the same
calculation path as forward_inputs.m01 (read-only REPEATABLE READ snapshot,
monthly count reconciliation, current_weights projection).

Writes <output-root>/<trade_date>/ once, atomically:

    targets.csv              native weights (signal_date, product, contract, ...)
    source-parameters.json   PRODUCTION parameters + lookback + signal_date
    precheck.json            specification and expiry checks (report only)
    manifest.json            status, source receipt, available_at, queries

`available_at` is this run's completion clock, never a file mtime. A package
that could not be produced is still written (status `not_produced` with the
reason) so the nightly records a mechanical target_missing; a later retry may
replace a `not_produced` package but never a `ready` one.
"""

import argparse
from dataclasses import replace
from datetime import date, timedelta
import json
import os
from pathlib import Path
import resource
import shutil
import socket
import subprocess
import sys

from citic_index.daily import LOOKBACK_DAYS, PRODUCTION
from citic_index.pipeline import build_from_panel, build_panel
from cta_carry.coverage import coverage_cutoff
from forward_account.contracts import digest, now_iso
from forward_inputs import m02_v2_specs as specs
from forward_inputs.m01 import (
    NATIVE_FILES,
    ROOT,
    ReadOnlySource,
    current_weights,
    file_sha,
    safe_failure_trace,
    scalar,
    write_json,
)

SCHEMA = "m02-v2-target-package.v1"
ADAPTER = "citic027_r150_t1"
# Files whose bytes decide the weights. Engine, driver and this module's
# packaging are deliberately outside: they do not change the target.
WEIGHT_FILES = NATIVE_FILES + ["forward_inputs/m01.py"]
MEMORY_LIMIT_BYTES = 2_000_000 * 1024


def weight_tree():
    return {name: file_sha(ROOT / name) for name in WEIGHT_FILES}


def source_version(tree=None):
    """Account-pinned identity: any change to the native calculation files
    changes it, and the engine then refuses the target."""
    return f"{ADAPTER}::m02-v2::{digest(tree or weight_tree())[:16]}"


def interval_for(calendar, trade_date):
    matches = [i for i in calendar["intervals"] if i["trade_date"] == trade_date]
    if len(matches) != 1:
        return None
    return matches[0]


def precheck(targets, trade_date, signal_date, table, settle_rows, info_rows, held):
    """Specification and expiry checks; reports, never edits the target.

    Specs are derived from S's rows exactly as the nightly will register them
    (a contract the account already holds was registered when first traded).
    Expiry guard: a held contract whose last trading day is on/before T while
    the target still holds it nonzero, or that is already past it.
    """
    settle = {(r["exchange"], r["contract_id"]): r for r in settle_rows}
    info = {(r["exchange"], r["contract_id"]): r for r in info_rows}
    weights = dict(zip(targets["contract"], targets["target_weight"]))
    missing, alerts = {}, []
    for contract, weight in sorted(weights.items()):
        if not weight or contract in held:
            continue
        try:
            exchange, _, _ = specs.split_code(contract)
            key = (exchange, specs.source_id(contract))
            specs.derive(contract, trade_date, table, settle.get(key), info.get(key))
        except specs.SpecMissing as exc:
            missing[contract] = str(exc)
    for contract in sorted(held):
        try:
            exchange, _, _ = specs.split_code(contract)
            row = info.get((exchange, specs.source_id(contract)))
        except specs.SpecMissing as exc:
            alerts.append({"contract": contract, "alert": str(exc)})
            continue
        if row is None:
            alerts.append({"contract": contract, "alert": "no contract-info row on S"})
            continue
        last = specs.last_trade_date(row)
        if last < trade_date:
            alerts.append(
                {"contract": contract, "last_trade_date": last, "alert": "held past last trading day"}
            )
        elif last == trade_date and weights.get(contract):
            alerts.append(
                {
                    "contract": contract,
                    "last_trade_date": last,
                    "alert": "last trading day today and the target has not rolled it out",
                }
            )
    return {
        "trade_date": trade_date,
        "signal_date": signal_date,
        "spec_table_sha256": table["table_sha256"],
        "nonzero_contracts": sum(1 for w in weights.values() if w),
        "spec_missing": missing,
        "expiry_alerts": alerts,
        "held_contracts": sorted(held),
        "ok": not missing and not alerts,
    }


def _held(store, account):
    if not store:
        return {}
    from forward_account.store import read_status

    return read_status(store, account)["state"]["positions"]


def run(trade_date, calendar, output_root, *, table, store=None, account=None):
    limit, _ = resource.getrlimit(resource.RLIMIT_AS)
    if limit == resource.RLIM_INFINITY or limit > MEMORY_LIMIT_BYTES:
        raise ValueError("run with a 2 GB virtual memory limit")
    interval = interval_for(calendar, trade_date)
    if interval is None:
        raise ValueError(f"{trade_date} is not a trading day of {calendar['calendar_id']}")
    signal = date.fromisoformat(interval["signal_date"])
    output_root = Path(output_root)
    final = output_root / trade_date
    if (final / "manifest.json").is_file():
        previous = json.loads((final / "manifest.json").read_text())
        if previous["status"] == "ready":
            raise ValueError(f"{final} is already ready; packages are immutable")
    work = output_root / f".{trade_date}.{os.getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    tree = weight_tree()
    manifest = {
        "schema": SCHEMA,
        "status": "not_produced",
        "reason": None,
        "trade_date": trade_date,
        "signal_date": signal.isoformat(),
        "interval": interval,
        "calendar_id": calendar["calendar_id"],
        "adapter": ADAPTER,
        "started_at": now_iso(),
        "available_at": None,
        "memory_limit_bytes": limit,
        "observer": {
            "host": socket.gethostname(),
            "checkout": str(ROOT),
            "git_head": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
            ).stdout.strip()
            or None,
        },
        "weight_files": tree,
        "source_version": source_version(tree),
        "lookback_days": LOOKBACK_DAYS,
    }
    source = ReadOnlySource()
    phase = "read_only_connection"
    try:
        with source:
            phase = "exchange_frontier"
            frontier = source.frontier(signal)
            manifest["data_frontier"] = {
                r["exchange"]: r["max_date"].isoformat()
                for r in frontier.to_dict("records")
            }
            try:
                cutoff = coverage_cutoff(
                    dict(zip(frontier["exchange"], frontier["max_date"]))
                ).cutoff
            except ValueError:
                cutoff = None
            if cutoff != signal:
                manifest["reason"] = "signal_date_mismatch"
                manifest["detail"] = (
                    f"five-exchange coverage cutoff {cutoff} != calendar signal date {signal}"
                )
                raise _NotProduced
            start = signal - timedelta(days=LOOKBACK_DAYS)
            config = replace(PRODUCTION, base_date=start)
            parameters = {k: scalar(v) for k, v in config.as_dict().items()}
            parameters.update(lookback_days=LOOKBACK_DAYS, signal_date=signal.isoformat())
            write_json(work / "source-parameters.json", parameters)
            phase = "native_900_day_input_capture"
            prices = source.window(start, signal, work)
            if prices.empty or prices["trade_date"].max() != signal:
                raise ValueError("normalized input frontier mismatch")
            manifest["source_window"] = {
                "start": start.isoformat(),
                "end": signal.isoformat(),
                "normalized_rows": len(prices),
                "observed_at": now_iso(),
            }
            phase = "native_target_calculation"
            manifest["calculation_started_at"] = now_iso()
            panel = build_panel(prices, config)
            result = build_from_panel(panel, config)
            del panel, prices
            targets = current_weights(result, signal, config)
            del result
            manifest["calculation_completed_at"] = now_iso()
            targets.to_csv(work / "targets.csv", index=False)
            phase = "specification_precheck"
            settle_rows = specs.fetch(source.query, specs.SETTLE_SQL, signal.isoformat())
            info_rows = specs.fetch(source.query, specs.INFO_SQL, signal.isoformat())
            try:
                check = precheck(
                    targets,
                    trade_date,
                    signal.isoformat(),
                    table,
                    settle_rows,
                    info_rows,
                    _held(store, account),
                )
            except Exception as exc:  # report-only: never withholds the target
                check = {
                    "ok": False,
                    "precheck_error": type(exc).__name__,
                    "failure_trace": safe_failure_trace(exc),
                }
            write_json(work / "precheck.json", check)
        phase = "source_integrity"
        if weight_tree() != tree:
            raise ValueError("weight files changed during calculation")
        manifest["status"] = "ready"
        manifest["available_at"] = now_iso()
        manifest["row_count"] = len(targets)
        manifest["gross_target_weight"] = float(targets["target_weight"].abs().sum())
        manifest["precheck_ok"] = check["ok"]
        manifest["source_receipt"] = {
            "source_version": manifest["source_version"],
            "available_at": manifest["available_at"],
            "csv_sha256": file_sha(work / "targets.csv"),
            "metadata_sha256": file_sha(work / "source-parameters.json"),
            "row_count": len(targets),
            "signal_date": signal.isoformat(),
            "synthetic": False,
        }
    except _NotProduced:
        manifest["failure_phase"] = phase
    except Exception as exc:
        manifest["reason"] = "not_produced"
        manifest["detail"] = f"{type(exc).__name__} during {phase}"
        manifest["failure_phase"] = phase
        manifest["failure_trace"] = safe_failure_trace(exc)
    manifest["queries"] = source.queries
    manifest["completed_at"] = now_iso()
    manifest["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    manifest["files"] = {p.name: file_sha(p) for p in sorted(work.iterdir()) if p.is_file()}
    write_json(work / "manifest.json", manifest)
    if final.exists():
        final.rename(output_root / f"{trade_date}.superseded-{os.getpid()}")
    os.replace(work, final)
    return manifest


class _NotProduced(Exception):
    pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--calendar", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--spec-table", default=str(specs.TABLE_PATH))
    parser.add_argument("--store")
    parser.add_argument("--account")
    args = parser.parse_args(argv)
    calendar = json.loads(Path(args.calendar).read_text())
    if interval_for(calendar, args.trade_date) is None:
        print(f"M02 v2 targets: {args.trade_date} is not a trading day; nothing to do")
        return 0
    manifest = run(
        args.trade_date,
        calendar,
        args.output_root,
        table=specs.load_table(args.spec_table),
        store=args.store,
        account=args.account,
    )
    print(
        json.dumps(
            {
                k: manifest.get(k)
                for k in ("trade_date", "signal_date", "status", "reason", "available_at", "row_count", "precheck_ok")
            },
            ensure_ascii=False,
        )
    )
    return 0 if manifest["status"] == "ready" else 2


if __name__ == "__main__":
    sys.exit(main())
