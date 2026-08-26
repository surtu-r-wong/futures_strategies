"""Excel, chart, and CLI integration tests for Carry."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import cta_carry.__main__ as carry_cli
import cta_carry.report as carry_report
from cta_carry.backtest import CarryBacktester
from cta_carry.minute_bars import MinuteDataError
from cta_carry.minute_pg_source import MinuteSourceAudit
from cta_carry.report import (
    ReportWriteError,
    console_summary,
    curve_selection_excel_view,
    write_carry_outputs,
)
from cta_carry.__main__ import _config_from_args, build_parser, main
from cta_carry.config import CarryConfig
from cta_carry.provenance import GitState
from tests.carry_fixtures import make_carry_panel, small_config


SHANGHAI = ZoneInfo("Asia/Shanghai")


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


def test_minute_report_adds_three_audit_sheets_before_run_config(tmp_path):
    result = replace(
        _result(),
        execution_mode="minute",
        executions=pd.DataFrame([{"execution_id": "e1"}]),
        intraday_stops=pd.DataFrame([{"execution_id": "e1"}]),
        minute_data_quality=pd.DataFrame([{"check": "coverage"}]),
    )

    xlsx, _ = write_carry_outputs(result, tmp_path / "minute")

    with pd.ExcelFile(xlsx, engine="openpyxl") as workbook:
        assert workbook.sheet_names == [
            "metrics",
            "daily_returns",
            "positions",
            "trades",
            "signals",
            "curve_selection",
            "data_quality",
            "executions",
            "intraday_stops",
            "minute_data_quality",
            "run_config",
        ]
    assert pd.read_excel(xlsx, sheet_name="executions").to_dict("records") == [
        {"execution_id": "e1"}
    ]
    assert pd.read_excel(xlsx, sheet_name="intraday_stops").to_dict("records") == [
        {"execution_id": "e1"}
    ]
    assert pd.read_excel(xlsx, sheet_name="minute_data_quality").to_dict("records") == [
        {"check": "coverage"}
    ]


def test_minute_report_writes_empty_audit_frames(tmp_path):
    result = replace(
        _result(),
        execution_mode="minute",
        executions=pd.DataFrame(columns=["execution_id"]),
        intraday_stops=pd.DataFrame(columns=["execution_id"]),
        minute_data_quality=pd.DataFrame(columns=["check"]),
    )

    xlsx, _ = write_carry_outputs(result, tmp_path / "minute_empty")

    with pd.ExcelFile(xlsx, engine="openpyxl") as workbook:
        assert workbook.sheet_names[-4:] == [
            "executions",
            "intraday_stops",
            "minute_data_quality",
            "run_config",
        ]
    assert pd.read_excel(xlsx, sheet_name="executions").empty
    assert pd.read_excel(xlsx, sheet_name="intraday_stops").empty
    assert pd.read_excel(xlsx, sheet_name="minute_data_quality").empty


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


def test_minute_report_preflights_audit_row_limit_before_opening_writer(
    tmp_path,
    monkeypatch,
):
    result = replace(
        _result(),
        execution_mode="minute",
        executions=pd.DataFrame(index=pd.RangeIndex(1_048_576)),
        intraday_stops=pd.DataFrame(),
        minute_data_quality=pd.DataFrame(),
    )
    writer_called = False

    def unexpected_writer(*args, **kwargs):
        nonlocal writer_called
        writer_called = True
        raise OSError("writer must not be opened")

    monkeypatch.setattr(carry_report.pd, "ExcelWriter", unexpected_writer)

    with pytest.raises(ReportWriteError) as exc_info:
        write_carry_outputs(result, tmp_path / "oversized_minute")

    error = exc_info.value
    assert error.stage == "preflight"
    assert error.sheet == "executions"
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


def test_runtime_config_includes_git_provenance(monkeypatch):
    state = GitState(
        version="abc123",
        dirty=True,
        diff_sha256="f" * 64,
    )
    monkeypatch.setattr(carry_cli, "capture_git_state", lambda root: state)

    runtime = dict(
        carry_cli._runtime_config(
            source="files",
            execution_mode="daily",
            products=["CU"],
            data=make_carry_panel(),
        )[["key", "value"]].itertuples(index=False)
    )

    assert runtime["code_version"] == "abc123"
    assert runtime["execution_mode"] == "daily"
    assert runtime["code_dirty"] is True
    assert runtime["code_diff_sha256"] == "f" * 64


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
        "4",
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


def test_cli_config_defaults_come_from_carry_config():
    """CLI research-parameter defaults are not duplicated: omitting every flag
    yields exactly CarryConfig()'s defaults, so a future default change in one
    place cannot silently diverge from the other."""
    args = build_parser().parse_args(["--start", "2020-01-01", "--end", "2020-12-31"])

    assert _config_from_args(args) == CarryConfig()


def test_cli_execution_defaults_to_daily():
    args = build_parser().parse_args(["--start", "2024-01-01", "--end", "2024-02-01"])

    assert args.execution == "daily"


def test_minute_execution_rejects_file_source_before_data_read(
    tmp_path,
    capsys,
    monkeypatch,
):
    def unexpected_data_read(*args, **kwargs):
        raise AssertionError("mode validation must precede file data access")

    monkeypatch.setattr(carry_cli.CarryDataSet, "from_dir", unexpected_data_read)

    code = main(
        [
            "--execution",
            "minute",
            "--source",
            "files",
            "--data-dir",
            str(tmp_path),
            "--start",
            "2024-01-01",
            "--end",
            "2024-02-01",
        ]
    )

    assert code == 2
    assert "--execution minute requires --source public-pg" in capsys.readouterr().err


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
    assert "code_dirty" in runtime
    assert "code_diff_sha256" in runtime
    code_dirty = str(runtime["code_dirty"]).lower()
    assert code_dirty in {"true", "false"}
    if code_dirty == "true":
        assert len(str(runtime["code_diff_sha256"])) == 64
    else:
        assert pd.isna(runtime["code_diff_sha256"]) or (
            runtime["code_diff_sha256"] == ""
        )
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


@pytest.mark.parametrize("execution", ["daily", "minute"])
def test_public_pg_cli_constructs_only_the_selected_execution_engine(
    tmp_path,
    monkeypatch,
    execution,
):
    data = make_carry_panel()
    base_result = _result()
    engine_calls = []
    source_calls = []
    rule_calls = []
    absence_calls = []
    basis_calls = []
    written_results = []

    class FakeDailyBacktester:
        def __init__(self, data, config, *, start, end):
            engine_calls.append(("daily", data, config, start, end))

        def run(self):
            return replace(base_result, execution_mode="daily")

    class FakeMinuteBacktester:
        def __init__(
            self,
            *,
            data,
            minute_source,
            session_rules,
            config,
            start,
            end,
            absent_product_days=(),
            pricing_bases=(),
        ):
            self.minute_source = minute_source
            engine_calls.append(
                (
                    "minute",
                    data,
                    minute_source,
                    session_rules,
                    config,
                    start,
                    end,
                )
            )
            absence_calls.append(tuple(absent_product_days))
            basis_calls.append(tuple(pricing_bases))

        def run(self):
            audit = self.minute_source.audit
            return replace(
                base_result,
                execution_mode="minute",
                run_config=pd.concat(
                    [
                        base_result.run_config,
                        pd.DataFrame(
                            [
                                {"key": "execution_mode", "value": "minute"},
                                {
                                    "key": "accounting_clock",
                                    "value": "piecewise_close_marked",
                                },
                                {
                                    "key": "minute_query_rules_version",
                                    "value": "timescale-bare-symbol-v1",
                                },
                                {
                                    "key": "session_rules_version",
                                    "value": "commodity-v1",
                                },
                                {
                                    "key": "multiplier_resolution_version",
                                    "value": "price-range-v1",
                                },
                                {
                                    "key": "minute_table_min",
                                    "value": audit.minute_table_min.isoformat(),
                                },
                                {
                                    "key": "minute_table_max",
                                    "value": audit.minute_table_max.isoformat(),
                                },
                                {
                                    "key": "minute_query_months",
                                    "value": audit.minute_query_months,
                                },
                                {
                                    "key": "minute_rows",
                                    "value": audit.minute_rows,
                                },
                                {
                                    "key": "minute_candidate_contract_days",
                                    "value": audit.minute_candidate_contract_days,
                                },
                            ]
                        ),
                    ],
                    ignore_index=True,
                ),
            )

    class FakeMinuteSource:
        def __init__(self, **kwargs):
            source_calls.append(("construct", kwargs))
            self.audit = MinuteSourceAudit(
                minute_table_min=datetime(2005, 1, 4, 9, 0, tzinfo=SHANGHAI),
                minute_table_max=datetime(2026, 8, 5, 23, 59, tzinfo=SHANGHAI),
                minute_query_months=7,
                minute_rows=123_456,
                minute_candidate_contract_days=89,
            )

        def load_table_bounds(self):
            source_calls.append(("bounds",))
            return self.audit.minute_table_min, self.audit.minute_table_max

    monkeypatch.setattr(carry_cli, "CarryBacktester", FakeDailyBacktester)
    monkeypatch.setattr(carry_cli, "CarryMinuteBacktester", FakeMinuteBacktester)
    monkeypatch.setattr(carry_cli, "PublicMinuteSource", FakeMinuteSource)
    monkeypatch.setattr(
        carry_cli,
        "load_session_rules",
        lambda path: rule_calls.append(path) or ("rule",),
    )
    monkeypatch.setattr(
        carry_cli,
        "load_public_carry_data",
        lambda **kwargs: data,
    )
    monkeypatch.setattr(
        carry_cli,
        "write_carry_outputs",
        lambda result, prefix: (
            written_results.append(result)
            or (tmp_path / "result.xlsx", tmp_path / "result.png")
        ),
    )
    monkeypatch.setattr(carry_cli, "console_summary", lambda result: "ok")
    args = _small_cli_args(
        None,
        tmp_path / execution,
        data.dates[12],
        data.dates[-1],
        source="public-pg",
    )
    args.extend(
        [
            "--execution",
            execution,
            "--settings",
            "settings.yaml",
            "--use-test",
        ]
    )

    assert main(args) == 0
    assert [call[0] for call in engine_calls] == [execution]
    final_config = written_results[0].run_config
    assert final_config["key"].eq("execution_mode").sum() == 1
    assert final_config.set_index("key").loc["execution_mode", "value"] == execution
    if execution == "daily":
        assert source_calls == []
        assert rule_calls == []
    else:
        assert source_calls == [
            (
                "construct",
                {"config_path": "settings.yaml", "use_test": True},
            ),
            ("bounds",),
        ]
        assert len(rule_calls) == 1
        assert rule_calls[0].name == "carry_minute_sessions.csv"
        # Without this the deferral built for unpriceable product-days never
        # reaches the engine, and a run crossing one fails closed instead.
        assert len(absence_calls) == 1
        # Zhengzhou fills cannot be priced on turnover; the engine must be told.
        assert [(row.exchange, row.basis) for row in basis_calls[0]] == [
            ("CZCE", "ohlc_typical")
        ]
        assert [
            (row.exchange, row.product, row.trade_date.isoformat())
            for row in absence_calls[0]
        ] == [
            ("SHFE", "AL", "2018-01-02"),
            ("SHFE", "CU", "2019-01-02"),
            ("SHFE", "RU", "2019-01-02"),
            ("SHFE", "AL", "2020-01-02"),
            ("SHFE", "FU", "2020-01-02"),
        ]
        final_values = dict(final_config[["key", "value"]].itertuples(index=False))
        assert final_values["minute_table_min"] == "2005-01-04T09:00:00+08:00"
        assert final_values["minute_table_max"] == "2026-08-05T23:59:00+08:00"
        assert type(final_values["minute_query_months"]) is int
        assert final_values["minute_query_months"] == 7
        assert type(final_values["minute_rows"]) is int
        assert final_values["minute_rows"] == 123_456
        assert type(final_values["minute_candidate_contract_days"]) is int
        assert final_values["minute_candidate_contract_days"] == 89


def test_minute_cli_rejects_result_missing_source_provenance(
    tmp_path,
    capsys,
    monkeypatch,
):
    data = make_carry_panel()
    base_result = _result()

    class FakeMinuteSource:
        def __init__(self, **kwargs):
            self.audit = MinuteSourceAudit(
                minute_table_min=datetime(2005, 1, 4, 9, 0, tzinfo=SHANGHAI),
                minute_table_max=datetime(2026, 8, 5, 23, 59, tzinfo=SHANGHAI),
                minute_query_months=7,
                minute_rows=123_456,
                minute_candidate_contract_days=89,
            )

        def load_table_bounds(self):
            return self.audit.minute_table_min, self.audit.minute_table_max

    class MissingProvenanceBacktester:
        def __init__(self, **kwargs):
            pass

        def run(self):
            return replace(base_result, execution_mode="minute")

    monkeypatch.setattr(carry_cli, "PublicMinuteSource", FakeMinuteSource)
    monkeypatch.setattr(
        carry_cli,
        "CarryMinuteBacktester",
        MissingProvenanceBacktester,
    )
    monkeypatch.setattr(
        carry_cli,
        "load_public_carry_data",
        lambda **kwargs: data,
    )
    monkeypatch.setattr(carry_cli, "load_session_rules", lambda path: ("rule",))
    monkeypatch.setattr(
        carry_cli,
        "write_carry_outputs",
        lambda result, prefix: (tmp_path / "result.xlsx", tmp_path / "result.png"),
    )
    monkeypatch.setattr(carry_cli, "console_summary", lambda result: "ok")
    args = _small_cli_args(
        None,
        tmp_path / "missing_provenance",
        data.dates[12],
        data.dates[-1],
        source="public-pg",
    )
    args.extend(["--execution", "minute"])

    assert main(args) == 2
    error = capsys.readouterr().err
    assert "minute_source_provenance" in error
    assert "accounting_clock" in error


def test_cli_returns_two_when_database_load_fails(tmp_path, capsys, monkeypatch):
    """A psycopg2 connection/query error is an expected failure mode: exit 2
    with a clean message, not an uncaught traceback."""
    import psycopg2

    def boom(**kwargs):
        raise psycopg2.OperationalError("could not connect to server")

    monkeypatch.setattr(carry_cli, "load_public_carry_data", boom)
    data = make_carry_panel()
    prefix = tmp_path / "db_failed"
    args = _small_cli_args(
        None,
        prefix,
        data.dates[12],
        data.dates[-1],
        source="public-pg",
    )

    exit_code = carry_cli.main(args)

    assert exit_code == 2
    assert "could not connect" in capsys.readouterr().err
    assert not prefix.with_suffix(".xlsx").exists()


def test_a_workbook_keeps_the_local_wall_time_of_an_aware_instant(tmp_path):
    from zoneinfo import ZoneInfo

    from cta_carry.report import _write_workbook

    # Excel has no concept of an offset, and the minute audit sheets are full
    # of aware instants -- a whole 2019-2020 run reached the last step and died
    # there. The instant is written as the wall time a Shanghai trader saw.
    shanghai = ZoneInfo("Asia/Shanghai")
    frame = pd.DataFrame(
        {
            "bar_time": [datetime(2020, 2, 3, 21, 5, tzinfo=shanghai)],
            "price": [606.5],
        }
    )
    path = tmp_path / "aware.xlsx"

    _write_workbook((("executions", frame),), path)

    written = pd.read_excel(path, sheet_name="executions")
    assert written["bar_time"].iloc[0] == pd.Timestamp("2020-02-03 21:05:00")
    assert written["price"].iloc[0] == 606.5


def test_cli_reports_the_notes_attached_to_a_failure(tmp_path, capsys, monkeypatch):
    """The minute source attaches notes when cleanup fails on top of an error.

    Printing only str(exc) throws them away, which is how a 55-minute run ended
    at "connection already closed" with nothing saying where.
    """
    import psycopg2

    def boom(**kwargs):
        error = psycopg2.InterfaceError("connection already closed")
        error.add_note("rollback also failed: InterfaceError('closed')")
        raise error

    monkeypatch.setattr(carry_cli, "load_public_carry_data", boom)
    data = make_carry_panel()
    prefix = tmp_path / "noted"
    args = _small_cli_args(
        None,
        prefix,
        data.dates[12],
        data.dates[-1],
        source="public-pg",
    )

    exit_code = carry_cli.main(args)

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "connection already closed" in err
    assert "rollback also failed" in err


def test_minute_cli_returns_two_when_stream_query_disconnects(
    tmp_path,
    capsys,
    monkeypatch,
):
    """A PostgreSQL failure during the minute engine is an expected CLI error."""
    import psycopg2

    data = make_carry_panel()

    class FakeMinuteSource:
        def __init__(self, **kwargs):
            self.audit = MinuteSourceAudit(
                minute_table_min=datetime(2005, 1, 4, 9, 0, tzinfo=SHANGHAI),
                minute_table_max=datetime(2026, 8, 5, 23, 59, tzinfo=SHANGHAI),
            )

        def load_table_bounds(self):
            return self.audit.minute_table_min, self.audit.minute_table_max

    class DisconnectingMinuteBacktester:
        def __init__(self, **kwargs):
            pass

        def run(self):
            raise psycopg2.OperationalError("stream disconnected")

    monkeypatch.setattr(carry_cli, "PublicMinuteSource", FakeMinuteSource)
    monkeypatch.setattr(
        carry_cli,
        "CarryMinuteBacktester",
        DisconnectingMinuteBacktester,
    )
    monkeypatch.setattr(
        carry_cli,
        "load_public_carry_data",
        lambda **kwargs: data,
    )
    monkeypatch.setattr(carry_cli, "load_session_rules", lambda path: ("rule",))
    prefix = tmp_path / "minute_db_failed"
    args = _small_cli_args(
        None,
        prefix,
        data.dates[12],
        data.dates[-1],
        source="public-pg",
    )
    args.extend(["--execution", "minute"])

    assert carry_cli.main(args) == 2
    assert "stream disconnected" in capsys.readouterr().err
    assert not prefix.with_suffix(".xlsx").exists()


def test_minute_cli_returns_two_for_structured_minute_data_failure(
    tmp_path,
    capsys,
    monkeypatch,
):
    data = make_carry_panel()

    class FailingMinuteSource:
        def __init__(self, **kwargs):
            pass

        def load_table_bounds(self):
            raise MinuteDataError(
                check="minute_table_bounds",
                reason="no exact table bounds",
            )

    monkeypatch.setattr(carry_cli, "PublicMinuteSource", FailingMinuteSource)
    monkeypatch.setattr(
        carry_cli,
        "load_public_carry_data",
        lambda **kwargs: data,
    )
    args = _small_cli_args(
        None,
        tmp_path / "minute_failed",
        data.dates[12],
        data.dates[-1],
        source="public-pg",
    )
    args.extend(["--execution", "minute"])

    assert main(args) == 2
    assert "minute_table_bounds" in capsys.readouterr().err


def test_minute_cli_returns_two_when_session_asset_cannot_be_loaded(
    tmp_path,
    capsys,
    monkeypatch,
):
    data = make_carry_panel()

    class FakeMinuteSource:
        def __init__(self, **kwargs):
            pass

        def load_table_bounds(self):
            return None

    monkeypatch.setattr(carry_cli, "PublicMinuteSource", FakeMinuteSource)
    monkeypatch.setattr(
        carry_cli,
        "load_public_carry_data",
        lambda **kwargs: data,
    )
    monkeypatch.setattr(
        carry_cli,
        "load_session_rules",
        lambda path: (_ for _ in ()).throw(
            FileNotFoundError("carry_minute_sessions.csv is missing")
        ),
    )
    args = _small_cli_args(
        None,
        tmp_path / "missing_sessions",
        data.dates[12],
        data.dates[-1],
        source="public-pg",
    )
    args.extend(["--execution", "minute"])

    assert main(args) == 2
    assert "carry_minute_sessions.csv is missing" in capsys.readouterr().err


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
