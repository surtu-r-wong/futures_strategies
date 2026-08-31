"""The Dow entry point: which knobs exist, and which windows it refuses."""

from __future__ import annotations

from datetime import date, datetime, time
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from common.commodity.bundle import write_bundle
from cta_dow.__main__ import (
    SESSION_RULES_LAST,
    SESSION_RULES_START,
    build_parser,
    main,
    resolve_options,
)


TZ = ZoneInfo("Asia/Shanghai")


def _args(tmp_path: Path) -> list[str]:
    return [
        "--panel-dir",
        str(tmp_path),
        "--start",
        "2012-01-04",
        "--end",
        "2026-01-30",
        "--output-prefix",
        str(tmp_path / "dow"),
    ]


def test_paper_defaults(tmp_path) -> None:
    options = resolve_options(build_parser().parse_args(_args(tmp_path)))

    assert options.ema_spans == (12, 26, 9)
    assert options.atr_window == 20
    assert options.signal_mode == "latched"
    assert options.target_vol == 0.15
    assert options.cost_bps == 1.3


def test_cli_rejects_the_session_rule_overrun(tmp_path) -> None:
    argv = _args(tmp_path)
    argv[argv.index("--end") + 1] = "2026-02-01"

    assert SESSION_RULES_LAST == date(2026, 1, 30)
    with pytest.raises(SystemExit, match="2026-01-30"):
        resolve_options(build_parser().parse_args(argv))


def test_cli_rejects_a_start_before_the_session_rules_begin(tmp_path) -> None:
    argv = _args(tmp_path)
    argv[argv.index("--start") + 1] = "2010-01-04"

    assert SESSION_RULES_START == date(2012, 1, 4)
    with pytest.raises(SystemExit, match="2012-01-04"):
        resolve_options(build_parser().parse_args(argv))


def test_the_paper_parameters_are_not_tuning_flags(tmp_path) -> None:
    for flag in ("--ema-fast", "--atr-window", "--target-vol", "--min-trades"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(_args(tmp_path) + [flag, "7"])


def test_a_literal_run_cannot_also_ask_for_the_literal_sensitivity(tmp_path) -> None:
    argv = _args(tmp_path) + ["--signal-mode", "literal", "--run-literal-sensitivity"]

    with pytest.raises(SystemExit, match="literal"):
        resolve_options(build_parser().parse_args(argv))


def test_the_sensitivity_run_gets_its_own_prefix(tmp_path) -> None:
    argv = _args(tmp_path) + ["--run-literal-sensitivity"]
    options = resolve_options(build_parser().parse_args(argv))

    assert options.run_literal_sensitivity is True
    assert Path(options.sensitivity_prefix).name == "dow_literal"


def _tiny_bundle(directory: Path) -> None:
    days = [stamp.date() for stamp in pd.bdate_range("2024-01-02", "2024-04-30")]
    rows = []
    for index, day in enumerate(days):
        for product, contract, multiplier, phase in (
            ("RB", "RB2405.SHF", 10, 0.0),
            ("TA", "TA405.CZC", 5, math.pi),
        ):
            slot_end = pd.Timestamp(datetime.combine(day, time(14, 45), tzinfo=TZ))
            close = 100.0 + 0.35 * index + 7.0 * math.sin(
                2 * math.pi * index / 25.0 + phase
            )
            rows.append(
                {
                    "product": product,
                    "contract": contract,
                    "trade_date": pd.Timestamp(day),
                    "slot_end": slot_end,
                    "open": close,
                    "high": close + 0.6,
                    "low": close - 0.6,
                    "close": close,
                    "volume": 100.0,
                    "open_interest": 1000.0 + index,
                    "no_trade": False,
                    "adj_factor": 1.0,
                "continuity_segment": 0,
                    "continuity_segment": 0,
                    "fill_time": slot_end + pd.Timedelta(minutes=5),
                    "fill_price": close,
                    "fill_pending": False,
                    "fill_unpriceable": False,
                    "pricing_basis": "amount_vwap",
                    "multiplier": multiplier,
                }
            )
    bars = pd.DataFrame(rows)
    months = sorted({day.replace(day=1) for day in days})
    universes = pd.DataFrame(
        [
            {"month_start": pd.Timestamp(month), "product": product}
            for month in months
            for product in ("RB", "TA")
        ]
    )
    dominants = pd.DataFrame(
        [
            {
                "trade_date": row.trade_date,
                "product": row.product,
                "contract": row.contract,
                "oi": 1000,
                "volume": 900,
                "selected_from": row.trade_date,
                "adj_factor": 1.0,
            }
            for row in bars.itertuples(index=False)
        ]
    )
    roll_fills = pd.DataFrame(
        columns=[
            "trade_date", "product", "old_contract", "new_contract", "fill_time",
            "old_price", "new_price", "old_pricing_basis", "new_pricing_basis",
        ]
    )
    write_bundle(
        directory,
        bars=bars,
        universes=universes,
        dominants=dominants,
        roll_fills=roll_fills,
        inputs={"fixture": "dow-cli"},
    )


def test_main_writes_a_faithful_and_a_literal_triplet(tmp_path) -> None:
    panel = tmp_path / "panel"
    prefix = tmp_path / "out" / "dow"
    _tiny_bundle(panel)

    code = main(
        [
            "--panel-dir",
            str(panel),
            "--start",
            "2024-01-02",
            "--end",
            "2024-04-30",
            "--output-prefix",
            str(prefix),
            "--require-paper-faithful",
            "--run-literal-sensitivity",
        ]
    )

    assert code == 0
    for stem in ("dow", "dow_literal"):
        base = prefix.parent / stem
        assert base.with_suffix(".xlsx").exists()
        assert base.with_suffix(".png").exists()
        assert base.with_suffix(".audit.json").exists()

    faithful = json.loads(prefix.with_suffix(".audit.json").read_text("utf-8"))
    literal = json.loads(
        (prefix.parent / "dow_literal").with_suffix(".audit.json").read_text("utf-8")
    )
    assert faithful["run_config"]["signal_mode"] == "latched"
    assert literal["run_config"]["signal_mode"] == "literal"
    assert faithful["sensitivity_only"] is False
    assert literal["sensitivity_only"] is True
    assert faithful["manifest"]["bundle_version"] == 1


def test_main_refuses_a_window_the_bundle_does_not_cover(tmp_path) -> None:
    panel = tmp_path / "panel"
    _tiny_bundle(panel)

    with pytest.raises(SystemExit, match="coverage"):
        main(
            [
                "--panel-dir",
                str(panel),
                "--start",
                "2024-01-02",
                "--end",
                "2024-09-30",
                "--output-prefix",
                str(tmp_path / "out" / "dow"),
            ]
        )
