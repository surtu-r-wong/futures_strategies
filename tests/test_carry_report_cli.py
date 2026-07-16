"""Excel, chart, and CLI integration tests for Carry."""

from dataclasses import replace
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pandas as pd
import pytest

import cta_carry.__main__ as carry_cli
import cta_carry.report as carry_report
from cta_carry.backtest import CarryBacktester
from cta_carry.report import (
    ReportWriteError,
    console_summary,
    curve_selection_excel_view,
    write_carry_outputs,
)
from cta_carry.__main__ import _config_from_args, build_parser, main
from tests.carry_fixtures import make_carry_panel, small_config


def _result():
    data = make_carry_panel()
    return CarryBacktester(
        data,
        config=small_config(),
        start=data.dates[12],
        end=data.dates[-1],
    ).run()


def test_report_writes_all_required_sheets_and_chart(tmp_path):
    result = _result()

    xlsx, png = write_carry_outputs(result, tmp_path / "carry_daily")

    assert png.exists()
    assert png.stat().st_size > 0
    assert pd.ExcelFile(xlsx).sheet_names == [
        "metrics",
        "daily_returns",
        "positions",
        "trades",
        "signals",
        "curve_selection",
        "data_quality",
        "run_config",
    ]
    metrics = pd.read_excel(xlsx, sheet_name="metrics").iloc[0]
    assert "calmar" in metrics.index
    first = pd.read_excel(xlsx, sheet_name="daily_returns").iloc[0]
    assert first["boundary_type"] == "report_start_initialization"


def test_curve_selection_excel_view_is_one_row_per_product_day():
    result = _result()

    view = curve_selection_excel_view(result.curve_selection)

    assert not view.duplicated(["trade_date", "product"]).any()
    assert {
        "candidate_contracts",
        "main_contract",
        "secondary_contract",
    }.issubset(view.columns)


def test_report_chart_uses_daily_gross_when_positions_are_empty(tmp_path):
    result = _result()
    result = replace(result, positions=result.positions.iloc[:0].copy())

    _, png = write_carry_outputs(result, tmp_path / "empty_positions")

    assert png.exists()
    assert png.stat().st_size > 0


def test_console_summary_includes_readiness_audit_cost_and_metrics():
    summary = console_summary(_result())

    for field in (
        "report_start=",
        "signal_ready=",
        "vol_ready=",
        "in_pool_product_days=",
        "excluded_product_days=",
        "trades=",
        "cost=",
        "ann_return=",
        "ann_vol=",
        "sharpe=",
        "calmar=",
        "max_drawdown=",
        "max_gross=",
    ):
        assert field in summary


def _sentinel_outputs(prefix):
    xlsx = Path(f"{prefix}.xlsx")
    png = Path(f"{prefix}_overview.png")
    xlsx.write_bytes(b"old-xlsx")
    png.write_bytes(b"old-png")
    return xlsx, png


def test_report_preflights_excel_row_limit_before_opening_writer(
    tmp_path,
    monkeypatch,
):
    result = replace(
        _result(),
        positions=pd.DataFrame(index=pd.RangeIndex(1_048_576)),
    )
    writer_called = False

    def unexpected_writer(*args, **kwargs):
        nonlocal writer_called
        writer_called = True
        raise OSError("writer must not be opened")

    monkeypatch.setattr(carry_report.pd, "ExcelWriter", unexpected_writer)

    with pytest.raises(ReportWriteError) as exc_info:
        write_carry_outputs(result, tmp_path / "oversized")

    error = exc_info.value
    assert error.stage == "preflight"
    assert error.sheet == "positions"
    assert error.rows == 1_048_576
    assert not writer_called
    assert list(tmp_path.iterdir()) == []


def test_excel_failure_preserves_sentinels_and_cleans_temporary_files(
    tmp_path,
    monkeypatch,
):
    prefix = tmp_path / "carry"
    xlsx, png = _sentinel_outputs(prefix)

    def failing_writer(path, *args, **kwargs):
        Path(path).write_bytes(b"partial-xlsx")
        raise OSError("excel failed")

    monkeypatch.setattr(carry_report.pd, "ExcelWriter", failing_writer)

    with pytest.raises(ReportWriteError) as exc_info:
        write_carry_outputs(_result(), prefix)

    assert exc_info.value.stage == "excel_write"
    assert xlsx.read_bytes() == b"old-xlsx"
    assert png.read_bytes() == b"old-png"
    assert set(tmp_path.iterdir()) == {xlsx, png}


def test_png_failure_preserves_sentinels_and_cleans_temporary_files(
    tmp_path,
    monkeypatch,
):
    prefix = tmp_path / "carry"
    xlsx, png = _sentinel_outputs(prefix)

    def failing_chart(result, path):
        Path(path).write_bytes(b"partial-png")
        raise OSError("png failed")

    monkeypatch.setattr(carry_report, "_write_overview_png", failing_chart)

    with pytest.raises(ReportWriteError) as exc_info:
        write_carry_outputs(_result(), prefix)

    assert exc_info.value.stage == "png_write"
    assert xlsx.read_bytes() == b"old-xlsx"
    assert png.read_bytes() == b"old-png"
    assert set(tmp_path.iterdir()) == {xlsx, png}


def test_dotted_output_prefix_is_preserved_verbatim(tmp_path):
    prefix = tmp_path / "carry.v1"

    xlsx, png = write_carry_outputs(_result(), prefix)

    assert xlsx == Path(f"{prefix}.xlsx")
    assert png == Path(f"{prefix}_overview.png")
    assert xlsx.exists()
    assert png.exists()


def test_console_pool_counts_do_not_rebuild_curve_excel_view(monkeypatch):
    def unexpected_view(frame):
        raise AssertionError("console summary must use direct pool counts")

    monkeypatch.setattr(
        carry_report,
        "curve_selection_excel_view",
        unexpected_view,
    )

    summary = carry_report.console_summary(_result())

    assert "in_pool_product_days=" in summary
    assert "excluded_product_days=" in summary


def test_git_version_uses_repo_root_and_timeout(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(stdout="abc123\n")

    monkeypatch.setattr(carry_cli.subprocess, "run", fake_run)

    assert carry_cli._git_version() == "abc123"
    assert captured["command"] == ["git", "rev-parse", "HEAD"]
    assert captured["cwd"] == Path(carry_cli.__file__).resolve().parents[1]
    assert captured["timeout"] > 0


def test_git_version_returns_unknown_on_timeout(monkeypatch):
    def time_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(carry_cli.subprocess, "run", time_out)

    assert carry_cli._git_version() == "unknown"


def _small_cli_args(
    data_dir,
    output_prefix,
    start,
    end,
    *,
    source="files",
):
    args = [
        "--source",
        source,
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
        "--products",
        "e,a,c,b,d",
        "--output-prefix",
        str(output_prefix),
        "--liquidity-window",
        "2",
        "--liquidity-threshold",
        "0",
        "--carry-window",
        "2",
        "--selection-fraction",
        "0.2",
        "--momentum-window",
        "2",
        "--atr-window",
        "2",
        "--atr-risk-budget",
        "0.005",
        "--vol-window",
        "4",
        "--min-shadow-active-days",
        "2",
        "--target-vol",
        "0.15",
        "--max-gross-leverage",
        "4",
        "--chandelier-atr-multiple",
        "2.5",
        "--stop-tranches",
        "3",
        "--cost-bps",
        "13",
        "--prewarm-calendar-days",
        "15",
    ]
    if data_dir is not None:
        args.extend(["--data-dir", str(data_dir)])
    return args


def test_cli_parser_exposes_every_carry_config_field(tmp_path):
    data = make_carry_panel()
    args = build_parser().parse_args(
        _small_cli_args(
            tmp_path,
            tmp_path / "carry",
            data.dates[12],
            data.dates[-1],
        )
    )

    assert _config_from_args(args) == small_config()


def test_file_cli_runs_writes_outputs_and_runtime_metadata(tmp_path):
    data = make_carry_panel()
    data_dir = tmp_path / "input"
    data_dir.mkdir()
    data.prices.to_csv(data_dir / "prices.csv", index=False)
    prefix = tmp_path / "output" / "carry"

    exit_code = main(
        _small_cli_args(
            data_dir,
            prefix,
            data.dates[12],
            data.dates[-1],
        )
    )

    assert exit_code == 0
    assert prefix.with_suffix(".xlsx").exists()
    assert prefix.with_name("carry_overview.png").exists()
    run_config = pd.read_excel(prefix.with_suffix(".xlsx"), sheet_name="run_config")
    runtime = dict(run_config[["key", "value"]].itertuples(index=False))
    assert runtime["source"] == "files"
    assert runtime["products"] == "A,B,C,D,E"
    assert runtime["code_version"]
    assert runtime["data_start_date"]
    assert runtime["data_end_date"]
    assert runtime["data_rows"] > 0


def test_public_pg_cli_forwards_products_config_and_connection_options(
    tmp_path,
    monkeypatch,
):
    data = make_carry_panel()
    captured = {}

    def fake_load_public_carry_data(**kwargs):
        captured.update(kwargs)
        return data

    monkeypatch.setattr(
        "cta_carry.__main__.load_public_carry_data",
        fake_load_public_carry_data,
    )
    prefix = tmp_path / "public_carry"
    args = _small_cli_args(
        None,
        prefix,
        data.dates[12],
        data.dates[-1],
        source="public-pg",
    )
    args.extend(["--settings", "settings.yaml", "--use-test"])

    exit_code = main(args)

    assert exit_code == 0
    assert captured == {
        "start": data.dates[12],
        "end": data.dates[-1],
        "config": small_config(),
        "products": ["A", "B", "C", "D", "E"],
        "config_path": "settings.yaml",
        "use_test": True,
    }


def test_cli_returns_nonzero_and_writes_no_success_report_on_warmup_error(
    tmp_path,
    capsys,
):
    data = make_carry_panel(periods=12)
    data_dir = tmp_path / "input"
    data_dir.mkdir()
    data.prices.to_csv(data_dir / "prices.csv", index=False)
    prefix = tmp_path / "carry_failed"

    exit_code = main(
        _small_cli_args(
            data_dir,
            prefix,
            data.dates[6],
            data.dates[-1],
        )
    )

    assert exit_code == 2
    assert "risk scaling not ready" in capsys.readouterr().err
    assert not prefix.with_suffix(".xlsx").exists()
    assert not prefix.with_name("carry_failed_overview.png").exists()


def test_cli_report_failure_returns_three_and_writes_no_outputs(
    tmp_path,
    capsys,
    monkeypatch,
):
    data = make_carry_panel()
    data_dir = tmp_path / "input"
    data_dir.mkdir()
    data.prices.to_csv(data_dir / "prices.csv", index=False)
    prefix = tmp_path / "failed_report"

    def fail_report(*args, **kwargs):
        raise ReportWriteError(
            stage="excel_write",
            reason="writer failed",
        )

    monkeypatch.setattr(carry_cli, "write_carry_outputs", fail_report)

    exit_code = carry_cli.main(
        _small_cli_args(
            data_dir,
            prefix,
            data.dates[12],
            data.dates[-1],
        )
    )

    assert exit_code == 3
    assert "writer failed" in capsys.readouterr().err
    assert not Path(f"{prefix}.xlsx").exists()
    assert not Path(f"{prefix}_overview.png").exists()


def test_cli_does_not_swallow_unexpected_runtime_errors(
    tmp_path,
    monkeypatch,
):
    data = make_carry_panel()
    data_dir = tmp_path / "input"
    data_dir.mkdir()
    data.prices.to_csv(data_dir / "prices.csv", index=False)
    prefix = tmp_path / "unexpected_failure"

    def explode(_self):
        raise RuntimeError("programming defect")

    monkeypatch.setattr(carry_cli.CarryBacktester, "run", explode)

    with pytest.raises(RuntimeError, match="programming defect"):
        carry_cli.main(
            _small_cli_args(
                data_dir,
                prefix,
                data.dates[12],
                data.dates[-1],
            )
        )


def test_publish_backup_failure_preserves_sentinels_and_cleans_temps(
    tmp_path,
    monkeypatch,
):
    prefix = tmp_path / "carry"
    xlsx, png = _sentinel_outputs(prefix)

    def failing_copy(_source, destination):
        Path(destination).write_bytes(b"partial-backup")
        raise OSError("backup failed")

    monkeypatch.setattr(carry_report.shutil, "copy2", failing_copy)

    with pytest.raises(ReportWriteError) as exc_info:
        write_carry_outputs(_result(), prefix)

    assert exc_info.value.stage == "publish"
    assert xlsx.read_bytes() == b"old-xlsx"
    assert png.read_bytes() == b"old-png"
    assert set(tmp_path.iterdir()) == {xlsx, png}


def test_second_publish_failure_restores_both_sentinels(
    tmp_path,
    monkeypatch,
):
    prefix = tmp_path / "carry"
    xlsx, png = _sentinel_outputs(prefix)
    real_replace = carry_report.os.replace

    def fail_second_publish(source, destination):
        source_path = Path(source)
        if Path(destination) == png and source_path.name.endswith(".tmp.png"):
            raise OSError("second publish failed")
        return real_replace(source, destination)

    monkeypatch.setattr(carry_report.os, "replace", fail_second_publish)

    with pytest.raises(ReportWriteError) as exc_info:
        write_carry_outputs(_result(), prefix)

    assert exc_info.value.stage == "publish"
    assert xlsx.read_bytes() == b"old-xlsx"
    assert png.read_bytes() == b"old-png"
    assert set(tmp_path.iterdir()) == {xlsx, png}


def test_rollback_failure_preserves_recovery_backup(
    tmp_path,
    monkeypatch,
):
    prefix = tmp_path / "carry"
    xlsx, png = _sentinel_outputs(prefix)
    real_replace = carry_report.os.replace

    def fail_publish_and_rollback(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == png and source_path.name.endswith(".tmp.png"):
            raise OSError("second publish failed")
        if destination_path == xlsx and source_path.name.endswith(".backup"):
            raise OSError("rollback failed")
        return real_replace(source, destination)

    monkeypatch.setattr(
        carry_report.os,
        "replace",
        fail_publish_and_rollback,
    )

    with pytest.raises(ReportWriteError) as exc_info:
        write_carry_outputs(_result(), prefix)

    error = exc_info.value
    assert error.stage == "publish"
    assert any("rollback failed" in item for item in error.secondary_errors)
    assert len(error.recovery_paths) == 1
    recovery_path = error.recovery_paths[0]
    assert recovery_path.exists()
    assert recovery_path.read_bytes() == b"old-xlsx"
    assert xlsx.read_bytes() != b"old-xlsx"
    assert png.read_bytes() == b"old-png"
    assert set(tmp_path.iterdir()) == {xlsx, png, recovery_path}


def test_primary_report_error_survives_temporary_cleanup_failure(
    tmp_path,
    monkeypatch,
):
    prefix = tmp_path / "carry"
    real_unlink = Path.unlink

    def fail_xlsx_temp_cleanup(path, *args, **kwargs):
        if path.name.endswith(".tmp.xlsx"):
            raise PermissionError("temp cleanup failed")
        return real_unlink(path, *args, **kwargs)

    def fail_chart(_result, _path):
        raise OSError("png failed")

    monkeypatch.setattr(Path, "unlink", fail_xlsx_temp_cleanup)
    monkeypatch.setattr(carry_report, "_write_overview_png", fail_chart)

    with pytest.raises(ReportWriteError) as exc_info:
        write_carry_outputs(_result(), prefix)

    error = exc_info.value
    assert error.stage == "png_write"
    assert any("temp cleanup failed" in item for item in error.secondary_errors)


def test_backup_cleanup_failure_is_structured(
    tmp_path,
    monkeypatch,
):
    prefix = tmp_path / "carry"
    _sentinel_outputs(prefix)
    real_unlink = Path.unlink

    def fail_backup_cleanup(path, *args, **kwargs):
        if path.name.endswith(".backup"):
            raise PermissionError("backup cleanup failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_cleanup)

    with pytest.raises(ReportWriteError) as exc_info:
        write_carry_outputs(_result(), prefix)

    error = exc_info.value
    assert error.stage == "cleanup"
    assert any("backup cleanup failed" in item for item in error.secondary_errors)


def test_sheet_preparation_failure_is_structured(tmp_path, monkeypatch):
    def fail_sheets(_result):
        raise KeyError("broken report shape")

    monkeypatch.setattr(carry_report, "_report_sheets", fail_sheets)

    with pytest.raises(ReportWriteError) as exc_info:
        write_carry_outputs(_result(), tmp_path / "carry")

    assert exc_info.value.stage == "prepare"
    assert "broken report shape" in str(exc_info.value)
