"""Excel, chart, and CLI integration tests for Carry."""

from dataclasses import replace

import pandas as pd

from cta_carry.backtest import CarryBacktester
from cta_carry.report import (
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
