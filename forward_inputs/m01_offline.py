"""Authorized diagnosis/reconstruction from one immutable M01 archive only."""

import argparse
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
import json
import os
from pathlib import Path
import resource
import socket
import sys
from unittest.mock import patch

import pandas as pd
import psycopg2

from citic_index import pg_source
from citic_index.daily import PRODUCTION, LOOKBACK_DAYS
from citic_index.pipeline import build_panel, build_from_panel
from cta_carry.data import normalize_contract_daily
from forward_account.contracts import digest, now_iso
from forward_inputs import m01
from forward_inputs.m01 import (
    ROOT,
    NATIVE_FILES,
    current_weights,
    file_sha,
    preview_evidence,
    safe_failure_trace,
    scalar,
    write_json,
)

ARCHIVE = ROOT / "output/forward_inputs/m01-20261015/current-target/source-daily.csv.gz"
PARAMETERS = ARCHIVE.parent / "source-parameters.json"
ARCHIVE_SHA256 = "9cf01433ea3157e686ef9c8f91a72c69f65f0cb15cc78a472b72d6f256f86b4e"
PARAMETERS_SHA256 = "fe2730f60fb5b2db2a6afb3189187009bca3917a05ffd00d9baf5470660af657"
PRODUCTION_OBSERVATION_SHA256 = (
    "99d6f71b2e7b0f4c0e77273c6abf17d080f0e36f49d6322120f3bffc099baefb"
)
SIGNAL_DATE = date(2026, 10, 15)


def deny_external(*args, **kwargs):
    raise RuntimeError(
        "offline diagnosis forbids network/database/source configuration"
    )


@contextmanager
def offline_only():
    """Deny the networking/database entry points used by this dependency chain."""
    with (
        patch.object(socket, "socket", deny_external),
        patch.object(socket, "create_connection", deny_external),
        patch.object(socket, "getaddrinfo", deny_external),
        patch.object(psycopg2, "connect", deny_external),
        patch.object(pg_source, "_dsn", deny_external),
        patch.object(m01, "_dsn", deny_external),
    ):
        yield


def validate_inputs():
    if file_sha(ARCHIVE) != ARCHIVE_SHA256:
        raise ValueError("authorized archive hash mismatch")
    if file_sha(PARAMETERS) != PARAMETERS_SHA256:
        raise ValueError("authorized parameter hash mismatch")
    params = json.loads(PARAMETERS.read_bytes())
    config = replace(PRODUCTION, base_date=SIGNAL_DATE - timedelta(days=LOOKBACK_DAYS))
    expected = {k: scalar(v) for k, v in config.as_dict().items()}
    expected.update(lookback_days=900, signal_date=SIGNAL_DATE.isoformat())
    if params != expected or LOOKBACK_DAYS != 900:
        raise ValueError("original production parameters/window changed")
    return config


def verified_sources():
    provenance = ARCHIVE.parent / "production-observation.json"
    if file_sha(provenance) != PRODUCTION_OBSERVATION_SHA256:
        raise ValueError("production observation changed")
    recorded = json.loads(provenance.read_bytes())["files"]
    native = {name: file_sha(ROOT / name) for name in NATIVE_FILES}
    if any(native[name] != recorded.get(name) for name in NATIVE_FILES):
        raise ValueError("native strategy source changed from verified production")
    native.update(
        {
            name: file_sha(ROOT / name)
            for name in (
                "forward_inputs/m01.py",
                "forward_inputs/m01_offline.py",
                "forward_account/contracts.py",
            )
        }
    )
    return native


def check_destination(path):
    path = Path(path).resolve()
    if path == ARCHIVE.parent.resolve() or ARCHIVE.parent.resolve() in path.parents:
        raise ValueError("output cannot be inside the original failed archive")
    if path.exists():
        raise FileExistsError("a new independent diagnostic directory is required")
    return path


def run(output_dir, purpose):
    limit, _ = resource.getrlimit(resource.RLIMIT_AS)
    if limit == resource.RLIM_INFINITY or limit > 2000000 * 1024:
        raise ValueError("run with a 2 GB virtual memory limit")
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("diagnostic purpose required")
    root = check_destination(output_dir)
    with offline_only():
        config = validate_inputs()
        sources = verified_sources()
        root.mkdir(parents=True, exist_ok=False)
        manifest = {
            "status": "blocked",
            "artifact_kind": "archived_input_offline_reconstruction",
            "purpose": purpose,
            "adapter": "citic027_r150_t1",
            "synthetic": False,
            "signal_date": SIGNAL_DATE.isoformat(),
            "started_at": now_iso(),
            "input_archive": str(ARCHIVE),
            "input_archive_sha256": ARCHIVE_SHA256,
            "parameters_sha256": PARAMETERS_SHA256,
            "source_files": sources,
            "source_tree_sha256": digest(sources),
            "memory_limit_bytes": limit,
            "lookback_days": 900,
            "database_calls": 0,
            "network_calls": 0,
            "network_database_guards": "socket, resolver, psycopg2 and live source config blocked",
            "actual_offline_pipeline_runs": 0,
            "source_historical_available_at": None,
            "first_seen_at": None,
            "available_at": None,
            "valid_execution_interval": None,
            "stages": [],
        }
        write_json(root / "run-start.json", manifest)
        phase = "archive_normalization"
        try:
            raw = pd.read_csv(ARCHIVE)
            manifest["archive_read_at"] = now_iso()
            manifest["raw_rows"] = len(raw)
            manifest["stages"].append({"phase": phase, "started_at": now_iso()})
            prices = normalize_contract_daily(raw).prices
            del raw
            if prices.empty or prices["trade_date"].max() != SIGNAL_DATE:
                raise ValueError("archived input frontier mismatch")
            manifest["normalized_rows"] = len(prices)
            manifest["stages"][-1]["completed_at"] = now_iso()
            phase = "native_panel"
            manifest["actual_offline_pipeline_runs"] = 1
            manifest["stages"].append({"phase": phase, "started_at": now_iso()})
            write_json(root / "pipeline-start.json", manifest)
            print("offline M01: archive normalized; native panel begins", flush=True)
            panel = build_panel(prices, config)
            manifest["stages"][-1]["completed_at"] = now_iso()
            phase = "native_factor_and_weights"
            manifest["stages"].append({"phase": phase, "started_at": now_iso()})
            result = build_from_panel(panel, config)
            del panel
            manifest["stages"][-1]["completed_at"] = now_iso()
            current = result.weights.loc[result.weights["trade_date"] == SIGNAL_DATE]
            legs = result.legs.loc[result.legs["trade_date"] == SIGNAL_DATE]
            # Current intermediate weights/contract identities only, never a historical return export.
            current.to_csv(root / "current-native-weights.csv", index=False)
            legs[["trade_date", "product", "main_contract", "main_close"]].to_csv(
                root / "current-native-legs.csv", index=False
            )
            manifest["projection_diagnostics"] = {
                "native_weight_rows": len(current),
                "zero_weight_products": current.loc[
                    current["weight"].eq(0), "product"
                ].tolist(),
                "missing_native_weights": int(current["weight"].isna().sum()),
                "current_dominant_rows": len(legs),
            }
            phase = "current_weight_projection"
            manifest["stages"].append({"phase": phase, "started_at": now_iso()})
            targets = current_weights(result, SIGNAL_DATE, config)
            manifest["stages"][-1]["completed_at"] = now_iso()
            del result
            phase = "offline_artifact_export"
            targets.to_csv(root / "targets.csv", index=False)
            manifest["first_seen_at"] = now_iso()
            (root / "source-parameters.json").write_bytes(PARAMETERS.read_bytes())
            evidence = preview_evidence(
                targets, prices, pd.DataFrame(), observed_at=manifest["archive_read_at"]
            )
            evidence["artifact_kind"] = manifest["artifact_kind"]
            evidence["price_origin"] = (
                "fixed archived public.futures_daily snapshot; no fresh quote"
            )
            evidence["metadata_status"] = "not_present_in_authorized_archive"
            write_json(root / "preview-evidence.json", evidence)
            if validate_inputs() != config or verified_sources() != sources:
                raise ValueError("authorized input/code changed during offline run")
            manifest["row_count"] = len(targets)
            manifest["gross_target_weight"] = float(
                targets["target_weight"].abs().sum()
            )
            manifest["net_target_weight"] = float(targets["target_weight"].sum())
            manifest["status"] = "offline_target_ready_execution_blocked"
            manifest["available_at"] = now_iso()
            manifest["source_version"] = "citic027-r150-t1:offline-20261015:" + digest(
                sources
            )
            manifest["source_receipt"] = {
                "source_version": manifest["source_version"],
                "available_at": manifest["available_at"],
                "csv_sha256": file_sha(root / "targets.csv"),
                "metadata_sha256": PARAMETERS_SHA256,
                "row_count": len(targets),
                "signal_date": SIGNAL_DATE.isoformat(),
                "synthetic": False,
            }
        except Exception as exc:
            manifest["failure_type"] = type(exc).__name__
            manifest["failure_phase"] = phase
            manifest["failure_trace"] = safe_failure_trace(exc)
            # Static adapter check labels only; never expose arbitrary source exceptions.
            known = {
                "missing dominant contract or weight": "missing_dominant_or_weight",
                "native volatility window is incomplete": "incomplete_native_volatility_window",
            }
            if type(exc) is ValueError and str(exc) in known:
                manifest["failure_check"] = known[str(exc)]
        manifest["completed_at"] = now_iso()
        manifest["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        manifest["files"] = {
            p.name: file_sha(p) for p in sorted(root.iterdir()) if p.is_file()
        }
        write_json(root / "manifest.pending.json", manifest)
        os.replace(root / "manifest.pending.json", root / "manifest.json")
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--purpose", required=True)
    args = parser.parse_args(argv)
    try:
        report = run(args.output_dir, args.purpose)
    except (ValueError, OSError, KeyError, TypeError):
        print(
            "offline M01: input/code verification or destination failed",
            file=sys.stderr,
        )
        return 2
    print(
        f"offline M01: {report['status']}; phase={report.get('failure_phase', 'complete')}"
    )
    return 0 if report["status"] == "offline_target_ready_execution_blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
