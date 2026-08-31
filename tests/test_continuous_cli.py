"""CLI 与端到端（计划 Task 8）。

§4 第 1 条：时段规则资产止于 2026-01-30，越界必须**报错退出**，不许悄悄截断 ——
截了之后拿到的是一段比以为的短的样本外，而那正是本仓最在意的一段。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from cta_continuous.__main__ import GRID, REVERSALS, load_panel, main, session_horizon
from cta_continuous.panel import PANEL_COLUMNS, normalise_panel

SHANGHAI = ZoneInfo("Asia/Shanghai")

_SESSION_HEADER = (
    "exchange,product,effective_start,effective_end,night_start,night_end,version"
)


def _sessions(tmp_path, *, end="2026-01-30"):
    path = tmp_path / "sessions.csv"
    path.write_text(
        f"{_SESSION_HEADER}\nSHFE,RB,2011-01-04,{end},21:00,23:00,commodity-v1\n",
        encoding="utf-8",
    )
    return path


def _panel_dir(tmp_path, *, months, bars=4):
    directory = tmp_path / "panel"
    directory.mkdir(exist_ok=True)
    for month in months:
        rows = []
        for offset in range(6):
            day = date(month.year, month.month, 1) + timedelta(days=offset)
            for index in range(bars):
                level = 1000.0 + 5.0 * (offset * bars + index) % 37
                slot = datetime(
                    day.year, day.month, day.day, 9, 15, tzinfo=SHANGHAI
                ) + timedelta(minutes=15) * index
                rows.append(
                    {
                        "product": "RB",
                        "contract": "RB2405.SHF",
                        "trade_date": day,
                        "slot_end": slot,
                        "open": level,
                        "high": level + 0.5,
                        "low": level - 0.5,
                        "close": level,
                        "volume": 100.0,
                        "no_trade": False,
                        "adj_factor": 1.0,
                        "continuity_segment": 0,
                        "fill_price": level,
                        "fill_pending": False,
                        "fill_unpriceable": False,
                        "pricing_basis": "amount_vwap",
                        "multiplier": 10,
                    }
                )
        frame = normalise_panel(pd.DataFrame(rows, columns=list(PANEL_COLUMNS)))
        frame.to_parquet(directory / f"panel-{month:%Y-%m}.parquet", index=False)
    return directory


def _argv(panel, sessions, prefix, *extra, start="2024-01", end="2024-02"):
    return [
        "--panel", str(panel),
        "--start", start,
        "--end", end,
        "--sessions", str(sessions),
        "--output-prefix", str(prefix),
        "--ema-short", "2",
        "--ema-long", "3",
        "--tnr-window", "2",
        "--atr-window", "2",
        "--dtnr-k", "2",
        "--min-observations", "2",
        *extra,
    ]


def test_cli_refuses_a_window_past_the_session_rule_horizon(tmp_path, capsys):
    """越过 2026-01-30 必须报错退出，不许悄悄截断（§4 第 1 条）。"""
    sessions = _sessions(tmp_path)
    panel = _panel_dir(tmp_path, months=[date(2024, 1, 1)])

    code = main(
        _argv(panel, sessions, tmp_path / "run", start="2024-01", end="2026-03")
    )

    assert code == 2
    message = capsys.readouterr().err
    assert message.startswith("session_horizon_exceeded:")
    assert "2026-01-30" in message
    # 报错就不许有任何产出 —— 有产出就等于「截断了但没说」。
    assert not list(tmp_path.glob("run*"))


def test_the_horizon_comes_from_the_asset_not_a_literal(tmp_path):
    assert session_horizon(_sessions(tmp_path)) == date(2026, 1, 30)
    assert session_horizon(_sessions(tmp_path, end="2025-06-30")) == date(2025, 6, 30)


def test_an_open_ended_rule_has_no_horizon_to_check_against(tmp_path):
    path = tmp_path / "open.csv"
    path.write_text(
        f"{_SESSION_HEADER}\nSHFE,RB,2011-01-04,,21:00,23:00,commodity-v1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as caught:
        session_horizon(path)

    assert str(caught.value).startswith("session_rules_open_ended:")


def test_cli_exposes_the_two_contradiction_switches(tmp_path):
    """D2 / D3：研报自相矛盾的两处都要能从命令行跑到另一侧。"""
    sessions = _sessions(tmp_path)
    panel = _panel_dir(tmp_path, months=[date(2024, 1, 1), date(2024, 2, 1)])

    assert main(
        _argv(
            panel, sessions, tmp_path / "rev",
            "--ma-orientation", "reversed", "--tnr-sign", "negative",
        )
    ) == 0

    summary = pd.read_csv(tmp_path / "rev-summary.csv")
    assert list(summary["ma_orientation"]) == ["reversed"]
    assert list(summary["tnr_sign"]) == ["negative"]


def test_cli_writes_a_workbook_an_audit_and_a_window_record(tmp_path):
    sessions = _sessions(tmp_path)
    panel = _panel_dir(tmp_path, months=[date(2024, 1, 1), date(2024, 2, 1)])

    assert main(_argv(panel, sessions, tmp_path / "run")) == 0

    assert (tmp_path / "run.xlsx").exists()
    assert (tmp_path / "run-audit.json").exists()
    window = pd.read_json(tmp_path / "run-window.json", typ="series")
    assert window["session_horizon"] == "2026-01-30"
    assert window["end"] == "2024-02-29"


def test_the_grid_is_the_preregistered_twelve(tmp_path):
    """D9：9 个网格点 + 3 个反向对照。**网格不得事后扩张**。"""
    assert len(GRID) == 9
    assert len(REVERSALS) == 3
    assert set(GRID) == {
        (short, long, window)
        for short, long in ((12, 26), (10, 60), (20, 120))
        for window in (10, 20, 40)
    }

    sessions = _sessions(tmp_path)
    panel = _panel_dir(tmp_path, months=[date(2024, 1, 1), date(2024, 2, 1)])

    assert main(_argv(panel, sessions, tmp_path / "grid", "--grid")) == 0

    summary = pd.read_csv(tmp_path / "grid-summary.csv")
    assert len(summary) == 12
    assert sum(label.startswith("reversal-") for label in summary["label"]) == 3
    # 样本外那三列必须在，且对每个点都有 —— 报告不许把它省掉。
    for column in ("out_of_sample_observations", "out_of_sample_sharpe"):
        assert column in summary.columns


def test_load_panel_refuses_a_directory_with_no_shards(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(ValueError) as caught:
        load_panel(empty, start=date(2024, 1, 1), end=date(2024, 2, 1))

    assert str(caught.value).startswith("panel_shards_missing:")


def test_load_panel_reads_only_the_requested_months(tmp_path):
    panel = _panel_dir(
        tmp_path, months=[date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)]
    )

    frame = load_panel(panel, start=date(2024, 2, 1), end=date(2024, 2, 1))

    assert set(frame["trade_date"].dt.month) == {2}
