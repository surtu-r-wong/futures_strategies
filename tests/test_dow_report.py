"""What the Dow workbook, chart, and audit file are required to say."""

from __future__ import annotations

from datetime import date, datetime, time
import json
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from common.commodity.backtest import BacktestResult
from cta_dow.report import (
    FIDELITY_RULE_IDS,
    IN_SAMPLE_END,
    PAPER_METRICS,
    REPORT_SHEETS,
    write_outputs,
)


TZ = ZoneInfo("Asia/Shanghai")


def _result() -> BacktestResult:
    days = [stamp.date() for stamp in pd.bdate_range("2022-06-01", "2022-09-30")]
    returns = [0.005 if index % 3 else -0.004 for index in range(len(days))]
    daily = pd.DataFrame(
        {
            "trade_date": days,
            "month_start": [day.replace(day=1) for day in days],
            "gross_return": returns,
            "turnover": [0.2] * len(days),
            "cost": [2.6e-5] * len(days),
            "direct_cost": [2.6e-5] * len(days),
            "net_return": returns,
            "gross_equity": np.cumprod([1.0 + value for value in returns]),
            "equity": np.cumprod([1.0 + value for value in returns]),
            "gross_leverage": [1.2] * len(days),
            "prevol_net_return": returns,
            "prevol_equity": np.cumprod([1.0 + value for value in returns]),
            "realized_vol": [0.18] * len(days),
            "vol_multiplier": [0.15 / 0.18] * len(days),
            "selected_count": [3] * len(days),
        }
    )
    day = days[0]
    positions = pd.DataFrame(
        [
            {
                "trade_date": day,
                "month_start": day.replace(day=1),
                "product": "RB",
                "contract": "RB2210.SHF",
                "selected": True,
                "active_products": 2,
                "universe_weight": 0.5,
                "base_weight_abs": 0.5,
                "direction": 1,
                "oi_scale": 1.0,
                "atr_leverage": 2.4,
                "realized_vol": 0.18,
                "target_annual_vol": 0.15,
                "vol_multiplier": 0.15 / 0.18,
                "leverage": 2.0,
                "target_weight": 1.0,
                "actual_weight": 1.0,
                "prevol_weight": 1.2,
            }
        ]
    )
    trades = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(datetime.combine(day, time(14, 50), tzinfo=TZ)),
                "trade_date": day,
                "product": "RB",
                "contract": "RB2210.SHF",
                "reason": "dow_entry",
                "price": 4000.0,
                "old_weight": 0.0,
                "new_weight": 1.0,
                "weight_change": 1.0,
                "turnover": 1.0,
                "cost": 1.3 / 10_000.0,
            }
        ]
    )
    signals = pd.DataFrame(
        [
            {
                "product": "RB",
                "trade_date": day,
                "month_start": day.replace(day=1),
                "slot_end": pd.Timestamp(datetime.combine(day, time(14, 45), tzinfo=TZ)),
                "contract": "RB2210.SHF",
                "trend": "up",
                "action": "dow_entry",
                "selected": True,
            }
        ]
    )
    selection = pd.DataFrame(
        [
            {
                "month_start": day.replace(day=1),
                "product": "RB",
                "in_liquidity_universe": True,
                "has_score": True,
                "observations": 252,
                "trade_count": 11,
                "cumulative_return": 0.33,
                "sharpe": 0.9,
                "calmar": 1.1,
                "eligible": True,
                "selected": True,
                "reason": "selected",
            }
        ]
    )
    quality = pd.DataFrame(
        [{"metric": "bars", "product": "RB", "value": 2048.0}],
        columns=["metric", "product", "value"],
    )
    return BacktestResult(
        daily=daily,
        positions=positions,
        trades=trades,
        signals=signals,
        selection=selection,
        data_quality=quality,
    )


@pytest.fixture
def result() -> BacktestResult:
    return _result()


def test_fidelity_contains_every_registered_rule() -> None:
    # fidelity_frame orders case-insensitively, so D comes before F.
    assert FIDELITY_RULE_IDS == (
        "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8",
        "F1", "F10", "F11", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9",
    )


def test_workbook_has_the_required_sheets(tmp_path, result) -> None:
    paths = write_outputs(result, output_prefix=tmp_path / "dow")

    assert pd.ExcelFile(paths.xlsx).sheet_names == list(REPORT_SHEETS)
    assert paths.png.exists()
    assert paths.audit.exists()


def test_metrics_split_at_the_paper_sample_end(tmp_path, result) -> None:
    paths = write_outputs(result, output_prefix=tmp_path / "dow")

    assert IN_SAMPLE_END == date(2022, 7, 29)
    metrics = pd.read_excel(paths.xlsx, sheet_name="metrics").set_index("period")
    assert pd.Timestamp(metrics.loc["in_sample", "end"]).date() <= IN_SAMPLE_END
    assert pd.Timestamp(metrics.loc["out_of_sample", "start"]).date() > IN_SAMPLE_END


def test_paper_metrics_sit_beside_the_replication(tmp_path, result) -> None:
    paths = write_outputs(result, output_prefix=tmp_path / "dow")

    assert PAPER_METRICS == {
        "annual_return": 0.2174,
        "sharpe": 1.42,
        "max_drawdown": 0.0990,
        "calmar": 2.20,
        "annual_volatility": 0.1534,
    }
    metrics = pd.read_excel(paths.xlsx, sheet_name="metrics").set_index("period")
    for name, value in PAPER_METRICS.items():
        assert metrics.loc["in_sample", f"paper_{name}"] == pytest.approx(value)
        assert pd.isna(metrics.loc["out_of_sample", f"paper_{name}"])


def test_a_literal_run_says_so_in_both_the_ledger_and_the_audit(tmp_path, result) -> None:
    paths = write_outputs(
        result, output_prefix=tmp_path / "dow_literal", sensitivity_only=True
    )

    fidelity = pd.read_excel(paths.xlsx, sheet_name="fidelity").set_index("rule_id")
    assert fidelity.loc["D6", "status"] == "sensitivity_only"
    assert fidelity.loc["D6", "variant"] == "literal_every_bar"
    audit = json.loads(paths.audit.read_text(encoding="utf-8"))
    assert audit["sensitivity_only"] is True


def test_the_faithful_run_keeps_the_latched_reading(tmp_path, result) -> None:
    paths = write_outputs(result, output_prefix=tmp_path / "dow")

    fidelity = pd.read_excel(paths.xlsx, sheet_name="fidelity").set_index("rule_id")
    assert fidelity.loc["D6", "status"] == "preregistered_default"
    assert "锁存" in fidelity.loc["D6", "implementation"]


def test_data_quality_counts_signals_cancelled_by_an_unavailable_fill() -> None:
    """保真度 F11 的那个数：策略真正想调仓、却没有对手盘的次数。

    与 `fill_unpriceable`（"这根 bar 的成交窗没人成交"）差着数量级 —— 后者在
    菜籽油 2012 那种死盘上有 688 根，而真正被作废的信号只有个位数。验收文档要写
    的是这一个。
    """
    import pandas as pd

    from common.commodity.backtest import _data_quality

    frame = pd.DataFrame(
        {
            "no_trade": [False, False, False, False],
            "action": ["dow_entry", "fill_unavailable", "hold", "fill_unavailable"],
        }
    )

    quality = _data_quality({"RB": frame}, [], {})

    row = quality.loc[
        quality["metric"] == "signals_cancelled_by_unavailable_fill"
    ].iloc[0]
    assert float(row["value"]) == 2.0
    assert row["product"] == "RB"


def test_data_quality_counts_forced_exits_priced_at_the_bars_close() -> None:
    """裁决 B 换了计价基准，验收文档要写条数 —— 报告层单列这一个数。"""
    import pandas as pd

    from common.commodity.backtest import _data_quality

    frame = pd.DataFrame(
        {
            # 命中数与不命中数故意不相等：否则把比较改成 `!=` 也能过。
            "no_trade": [False, False, False, False, False],
            "action": [
                "dow_entry",
                "continuity_break_close",
                "continuity_break",
                "continuity_break_close",
                "hold",
            ],
        }
    )

    quality = _data_quality({"RB": frame}, [], {})

    row = quality.loc[quality["metric"] == "forced_exits_priced_at_close"].iloc[0]
    assert float(row["value"]) == 2.0
    assert row["product"] == "RB"
