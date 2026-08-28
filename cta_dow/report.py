"""Workbook, chart, and audit archive for the Dow replication.

Only this replication's constants live here: the paper's annual table ends
2022-07-29, its headline is 21.74% / 1.42 / 9.90% / 2.20 / 15.34%, and the
registered sensitivity is the every-bar reading of the entry gates. The
sheets, the sample-cutoff chart, the Excel guard, and the audit archive are
shared with the Bollinger replication in ``common.commodity.report``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from common.commodity.report import (
    REPORT_SHEETS,
    SHARED_FIDELITY_ROWS,
    OutputPaths,
    ReportSpec,
    build_sheets as _build_sheets,
    write_outputs as _write_outputs,
)
from common.commodity.reporting import fidelity_frame

__all__ = [
    "FIDELITY_RULE_IDS",
    "IN_SAMPLE_END",
    "PAPER_METRICS",
    "REPORT_SHEETS",
    "OutputPaths",
    "build_sheets",
    "write_outputs",
]


#: 研报年度表明确标注到此；其后为样本外。
IN_SAMPLE_END = date(2022, 7, 29)

#: 研报正文口径（年化收益 / Sharpe / 最大回撤 / Calmar / 年化波动）。
PAPER_METRICS: dict[str, float] = {
    "annual_return": 0.2174,
    "sharpe": 1.42,
    "max_drawdown": 0.0990,
    "calmar": 2.20,
    "annual_volatility": 0.1534,
}

_SETTINGS: dict[str, object] = {
    "target_annual_vol": 0.15,
    "cost_bps": 1.3,
    "ema_fast": 12,
    "ema_slow": 26,
    "ema_signal": 9,
    "atr_window": 20,
    "segment_history_depth": 2,
    "signal_mode": "latched",
    "selection_min_trades": 5,
    "selection_observations": 252,
    "realized_vol_observations": 252,
}

_FIDELITY_ROWS: tuple[dict[str, str], ...] = SHARED_FIDELITY_ROWS + (
    {
        "rule_id": "D1",
        "paper_text": "MACD 指标",
        "implementation": "EMA(12)/EMA(26)/EMA(9)，adjust=False，首项取原值",
        "basis": "研报只给了 MACD 名称，未披露参数与平滑口径",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "直接决定累计穿越距离与趋势切换时点",
    },
    {
        "rule_id": "D2",
        "paper_text": "累计穿越距离与 ATR 比较",
        "implementation": "±ATR 之间保持上一初步趋势；首次触发前为中性",
        "basis": "研报只画了两条阈值线，未写阈值之间怎么办",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "避免在阈值带内反复切段",
    },
    {
        "rule_id": "D3",
        "paper_text": "趋势段的最高价与最低价",
        "implementation": "段边界只由初步趋势划分，拐点修正不重新切段",
        "basis": "用修正后趋势切段会让极值自我改写，比较对象随之漂移",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "极值历史保持可复现",
    },
    {
        "rule_id": "D4",
        "paper_text": "拐点修正",
        "implementation": "条件失效只平掉同向仓位，不直接反手",
        "basis": "「本轮回撤跌破前低」说的是上升趋势不再成立，不是下降趋势已确立",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "避免把修正条件误写成高频反手信号",
    },
    {
        "rule_id": "D5",
        "paper_text": "收盘价突破临时极值",
        "implementation": "先比较当前收盘与本 bar 之前的临时极值，再把本 bar 纳入极值",
        "basis": "先更新极值则该条件退化为「收盘恰等于本 bar 最高价」",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "决定入场 bar 的判定，是最容易写反的一处",
    },
    {
        "rule_id": "D6",
        "paper_text": "满足共振条件后入场，持有至趋势失效",
        "implementation": "入场需要一次新的收盘突破，之后锁存到趋势切换或拐点失效",
        "basis": "突破是事件不是状态；逐 bar 重验会在未创新高的每根 bar 上平仓再进",
        "status": "preregistered_default",
        "variant": "literal_every_bar",
        "impact": "锁存与逐 bar 两种读法的换手与成本相差很大，敏感性单独出产物",
    },
    {
        "rule_id": "D7",
        "paper_text": "资金在满足开仓条件的品种间等权分配",
        "implementation": "只在当前实际持仓品种间等分；无信号品种不占分母",
        "basis": "研报写的是当前持仓品种等权，不是全部入选品种等权",
        "status": "paper_explicit",
        "variant": "none",
        "impact": "任一品种进出都会改变其余品种的目标，各自在自己下一窗口调整",
    },
    {
        "rule_id": "D8",
        "paper_text": "ATR 阈值",
        "implementation": "ATR 未满窗或为零的 bar 不判定趋势，沿用上一趋势且不交易",
        "basis": "零阈值会让任何累计距离都成为触发；研报未写涨跌停锁死怎么办",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "极少数锁死时段不建仓，而不是整段回测硬失败",
    },
)

FIDELITY_RULE_IDS: tuple[str, ...] = tuple(
    fidelity_frame(_FIDELITY_ROWS)["rule_id"].tolist()
)


def _fidelity_sheet(sensitivity_only: bool) -> pd.DataFrame:
    rows = []
    for row in _FIDELITY_ROWS:
        copied = dict(row)
        if sensitivity_only and copied["rule_id"] == "D6":
            copied["implementation"] = "逐 bar 重验全部三道闸，不锁存"
            copied["status"] = "sensitivity_only"
        rows.append(copied)
    return fidelity_frame(rows)


SPEC = ReportSpec(
    title="Guosen Dow replication",
    in_sample_end=IN_SAMPLE_END,
    paper_metrics=PAPER_METRICS,
    fidelity=_fidelity_sheet,
    settings=_SETTINGS,
)


def build_sheets(
    result: Any,
    *,
    universe: pd.DataFrame | None = None,
    dominant_rolls: pd.DataFrame | None = None,
    run_config: Mapping[str, object] | None = None,
    sensitivity_only: bool = False,
) -> dict[str, pd.DataFrame]:
    """Every sheet the Dow workbook writes, in the order it writes them."""
    return _build_sheets(
        result,
        spec=SPEC,
        universe=universe,
        dominant_rolls=dominant_rolls,
        run_config=run_config,
        sensitivity_only=sensitivity_only,
    )


def write_outputs(
    result: Any,
    *,
    output_prefix: str | Path,
    universe: pd.DataFrame | None = None,
    dominant_rolls: pd.DataFrame | None = None,
    run_config: Mapping[str, object] | None = None,
    manifest: Mapping[str, object] | None = None,
    query_plans: Sequence[Mapping[str, object]] | None = None,
    sensitivity_only: bool = False,
) -> OutputPaths:
    """Write the workbook, the chart, and the audit archive for one run."""
    return _write_outputs(
        result,
        spec=SPEC,
        output_prefix=output_prefix,
        universe=universe,
        dominant_rolls=dominant_rolls,
        run_config=run_config,
        manifest=manifest,
        query_plans=query_plans,
        sensitivity_only=sensitivity_only,
    )
