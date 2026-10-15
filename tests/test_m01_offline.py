"""Offline diagnostics must neither fetch data nor alter archived inputs."""

import hashlib
import json
import socket

import pandas as pd
import psycopg2
import pytest

from forward_inputs import m01_offline as offline


def test_offline_guard_denies_network_database_and_source_configuration():
    from citic_index import pg_source
    from forward_inputs import m01

    before = socket.socket
    with offline.offline_only():
        for action in (
            lambda: socket.socket(),
            lambda: socket.getaddrinfo("example.invalid", 80),
            lambda: psycopg2.connect("synthetic"),
            lambda: pg_source._dsn(),
            lambda: m01._dsn(),
        ):
            with pytest.raises(RuntimeError, match="offline"):
                action()
    assert socket.socket is before


def test_changed_archive_or_parameters_rejected_before_computation(
    tmp_path, monkeypatch
):
    archive = tmp_path / "source.csv.gz"
    params = tmp_path / "params.json"
    archive.write_bytes(b"synthetic")
    params.write_text("{}")
    monkeypatch.setattr(offline, "ARCHIVE", archive)
    monkeypatch.setattr(offline, "PARAMETERS", params)
    with pytest.raises(ValueError, match="archive"):
        offline.validate_inputs()
    monkeypatch.setattr(
        offline, "ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    with pytest.raises(ValueError, match="parameter"):
        offline.validate_inputs()


def test_output_cannot_be_inside_failed_directory(tmp_path, monkeypatch):
    original = tmp_path / "original"
    original.mkdir()
    monkeypatch.setattr(offline, "ARCHIVE", original / "source-daily.csv.gz")
    with pytest.raises(ValueError, match="original"):
        offline.check_destination(original / "new")
    (tmp_path / "used").mkdir()
    with pytest.raises(FileExistsError):
        offline.check_destination(tmp_path / "used")


def test_offline_entry_runs_mock_pipeline_without_live_source(tmp_path, monkeypatch):
    from datetime import date
    from dataclasses import replace
    from types import SimpleNamespace
    from citic_index.daily import PRODUCTION
    from forward_inputs.m01 import scalar

    archive = tmp_path / "original" / "source-daily.csv.gz"
    archive.parent.mkdir()
    frame = pd.DataFrame(
        {
            "trade_date": ["2024-04-28", "2026-10-15"],
            "contract": ["M2409.DCE", "M2701.DCE"],
            "close": [100, 110],
        }
    )
    frame.to_csv(archive, index=False, compression="gzip")
    params = archive.parent / "source-parameters.json"
    config = replace(PRODUCTION, base_date=date(2024, 4, 28))
    parameter_data = {k: scalar(v) for k, v in config.as_dict().items()}
    parameter_data.update(signal_date="2026-10-15", lookback_days=900)
    params.write_text(json.dumps(parameter_data))
    monkeypatch.setattr(offline, "ARCHIVE", archive)
    monkeypatch.setattr(offline, "PARAMETERS", params)
    monkeypatch.setattr(
        offline, "ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(
        offline, "PARAMETERS_SHA256", hashlib.sha256(params.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(offline, "verified_sources", lambda: {"synthetic": "a" * 64})
    monkeypatch.setattr(
        offline.resource, "getrlimit", lambda key: (2000000 * 1024,) * 2
    )
    monkeypatch.setattr(
        offline,
        "normalize_contract_daily",
        lambda raw: SimpleNamespace(
            prices=raw.assign(trade_date=pd.to_datetime(raw.trade_date).dt.date)
        ),
    )
    monkeypatch.setattr(offline, "build_panel", lambda prices, c: prices)
    end = date(2026, 10, 15)
    native = SimpleNamespace(
        weights=pd.DataFrame({"trade_date": [end], "product": ["M"], "weight": [0.5]}),
        legs=pd.DataFrame(
            {
                "trade_date": [end],
                "product": ["M"],
                "main_contract": ["M2701.DCE"],
                "main_close": [110],
            }
        ),
    )
    monkeypatch.setattr(offline, "build_from_panel", lambda panel, c: native)
    targets = pd.DataFrame(
        {
            "signal_date": [end],
            "product": ["M"],
            "contract": ["M2701.DCE"],
            "raw_weight": [0.5],
            "vol_scale": [2.0],
            "target_weight": [1.0],
            "signal_close_reference": [110],
        }
    )
    monkeypatch.setattr(offline, "current_weights", lambda result, end, c: targets)
    before = archive.read_bytes()
    report = offline.run(tmp_path / "reconstructed", "synthetic entry isolation")
    assert report["status"] == "offline_target_ready_execution_blocked"
    assert report["artifact_kind"] == "archived_input_offline_reconstruction"
    assert report["source_historical_available_at"] is None
    assert report["available_at"] and report["first_seen_at"]
    assert report["input_archive_sha256"] == hashlib.sha256(before).hexdigest()
    assert archive.read_bytes() == before
    assert report["database_calls"] == report["network_calls"] == 0
    assert (tmp_path / "reconstructed" / "targets.csv").is_file()
