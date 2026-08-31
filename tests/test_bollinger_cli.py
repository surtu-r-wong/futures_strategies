"""The Bollinger entry point: which knobs exist, and which windows it refuses."""

from __future__ import annotations

from datetime import date, datetime, time
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from common.commodity.bundle import write_bundle
from cta_bollinger.__main__ import (
    SESSION_RULES_LAST,
    SESSION_RULES_START,
    build_parser,
    main,
    resolve_options,
)


TZ = ZoneInfo("Asia/Shanghai")


def _args(tmp_path: Path, **overrides) -> list[str]:
    argv = [
        "--panel-dir",
        str(tmp_path),
        "--start",
        "2012-01-04",
        "--end",
        "2026-01-30",
        "--output-prefix",
        str(tmp_path / "bollinger"),
    ]
    for flag, value in overrides.items():
        argv.extend([f"--{flag.replace('_', '-')}", value] if value else [f"--{flag.replace('_', '-')}"])
    return argv


def test_paper_defaults(tmp_path) -> None:
    options = resolve_options(build_parser().parse_args(_args(tmp_path)))

    assert options.length == 300
    assert options.beta == 1.5
    assert options.ddof == 0
    assert options.target_vol == 0.10
    assert options.cost_bps == 1.3
    assert options.atr_window == 20
    assert options.oi_short == 150
    assert options.oi_long == 300


def test_cli_rejects_the_session_rule_overrun(tmp_path) -> None:
    argv = _args(tmp_path)
    argv[argv.index("--end") + 1] = "2026-02-01"
    namespace = build_parser().parse_args(argv)

    assert SESSION_RULES_LAST == date(2026, 1, 30)
    with pytest.raises(SystemExit, match="2026-01-30"):
        resolve_options(namespace)


def test_cli_rejects_a_start_before_the_session_rules_begin(tmp_path) -> None:
    argv = _args(tmp_path)
    argv[argv.index("--start") + 1] = "2010-01-04"
    namespace = build_parser().parse_args(argv)

    assert SESSION_RULES_START == date(2012, 1, 4)
    with pytest.raises(SystemExit, match="2012-01-04"):
        resolve_options(namespace)


def test_cli_rejects_an_inverted_window(tmp_path) -> None:
    argv = _args(tmp_path)
    argv[argv.index("--start") + 1] = "2025-01-02"
    argv[argv.index("--end") + 1] = "2024-01-02"

    with pytest.raises(SystemExit, match="--end"):
        resolve_options(build_parser().parse_args(argv))


def test_the_paper_parameters_are_not_tuning_flags(tmp_path) -> None:
    for flag in ("--length", "--beta", "--target-vol", "--take-profit-std", "--oi-short"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(_args(tmp_path) + [flag, "7"])


def test_a_sensitivity_run_cannot_duplicate_the_faithful_one(tmp_path) -> None:
    argv = _args(tmp_path) + ["--ddof", "1", "--run-ddof-sensitivity"]

    with pytest.raises(SystemExit, match="ddof"):
        resolve_options(build_parser().parse_args(argv))


def test_the_sensitivity_run_gets_its_own_prefix(tmp_path) -> None:
    argv = _args(tmp_path) + ["--run-ddof-sensitivity"]
    options = resolve_options(build_parser().parse_args(argv))

    assert options.run_ddof_sensitivity is True
    assert Path(options.sensitivity_prefix).name == "bollinger_ddof1"


def _tiny_bundle(directory: Path, *, unpriceable_days: int = 0) -> None:
    days = [stamp.date() for stamp in pd.bdate_range("2024-01-02", "2024-02-29")]
    rows = []
    for index, day in enumerate(days):
        for product, contract, multiplier in (
            ("RB", "RB2405.SHF", 10),
            ("TA", "TA405.CZC", 5),
        ):
            slot_end = pd.Timestamp(datetime.combine(day, time(14, 45), tzinfo=TZ))
            close = 100.0 + (index % 5)
            rows.append(
                {
                    "product": product,
                    "contract": contract,
                    "trade_date": pd.Timestamp(day),
                    "slot_end": slot_end,
                    "open": close,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "volume": 100.0,
                    "open_interest": 1000.0 + index,
                    "no_trade": False,
                    "adj_factor": 1.0,
                "continuity_segment": 0,
                    "continuity_segment": 0,
                    "fill_time": slot_end + pd.Timedelta(minutes=5),
                    "fill_price": (
                        float("nan") if index < unpriceable_days else close
                    ),
                    "fill_pending": False,
                    "fill_unpriceable": index < unpriceable_days,
                    "pricing_basis": "amount_vwap",
                    "multiplier": multiplier,
                }
            )
    bars = pd.DataFrame(rows)
    universes = pd.DataFrame(
        [
            {"month_start": pd.Timestamp(month), "product": product}
            for month in (date(2024, 1, 1), date(2024, 2, 1))
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
            "trade_date",
            "product",
            "old_contract",
            "new_contract",
            "fill_time",
            "old_price",
            "new_price",
            "old_pricing_basis",
            "new_pricing_basis",
        ]
    )
    write_bundle(
        directory,
        bars=bars,
        universes=universes,
        dominants=dominants,
        roll_fills=roll_fills,
        inputs={"fixture": "cli"},
    )


def test_main_writes_a_faithful_and_a_sensitivity_triplet(tmp_path) -> None:
    panel = tmp_path / "panel"
    prefix = tmp_path / "out" / "bollinger"
    _tiny_bundle(panel)

    code = main(
        [
            "--panel-dir",
            str(panel),
            "--start",
            "2024-01-02",
            "--end",
            "2024-02-29",
            "--output-prefix",
            str(prefix),
            "--require-paper-faithful",
            "--run-ddof-sensitivity",
        ]
    )

    assert code == 0
    for stem in ("bollinger", "bollinger_ddof1"):
        base = prefix.parent / stem
        assert base.with_suffix(".xlsx").exists()
        assert base.with_suffix(".png").exists()
        assert base.with_suffix(".audit.json").exists()

    faithful = json.loads((prefix.with_suffix(".audit.json")).read_text("utf-8"))
    sensitivity = json.loads(
        (prefix.parent / "bollinger_ddof1").with_suffix(".audit.json").read_text("utf-8")
    )
    assert faithful["sensitivity_only"] is False
    assert sensitivity["sensitivity_only"] is True
    assert faithful["run_config"]["ddof"] == 0
    assert sensitivity["run_config"]["ddof"] == 1
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
                "2024-06-28",
                "--output-prefix",
                str(tmp_path / "out" / "bollinger"),
            ]
        )


def test_an_unpriceable_window_is_counted_not_refused(tmp_path) -> None:
    """闸的含义收到「策略真正要成交的那一笔」上。

    安静时段里没人成交的成交窗口在全历史上必然存在（三个月十四个品种的探针就有
    20 根、占 0.16%，还都落在 AL/P/Y/A 这类流动品种），按「区间内任何一根不可定价
    即拒」这条判据，注册的验收命令永远跑不了。真正该硬失败的是**策略确实要换仓、
    而那一笔定不出价** —— 那条在影子里逐 bar 生效，与本开关无关。
    """
    _tiny_bundle(tmp_path, unpriceable_days=2)

    exit_code = main(
        [
            "--panel-dir",
            str(tmp_path),
            "--start",
            "2024-01-02",
            "--end",
            "2024-02-29",
            "--output-prefix",
            str(tmp_path / "bollinger"),
            "--require-paper-faithful",
        ]
    )

    assert exit_code == 0
    audit = json.loads((tmp_path / "bollinger.audit.json").read_text(encoding="utf-8"))
    assert audit["run_config"]["unpriceable_fill_windows"] == 4
