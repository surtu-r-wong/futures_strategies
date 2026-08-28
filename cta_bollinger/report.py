"""Workbook, chart, and audit archive for the Bollinger replication.

Only this replication's constants live here: the paper's sample ends
2021-09-30, its headline is 17.52% / 1.72 / 8.27% / 2.12 / 10.16%, and the
registered sensitivity is the undisclosed standard-deviation convention. The
sheets, the chart, the Excel guard, and the audit archive are shared with the
Dow replication in ``common.commodity.report``.
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
from cta_bollinger.backtest import BacktestResult

__all__ = [
    "FIDELITY_RULE_IDS",
    "IN_SAMPLE_END",
    "PAPER_METRICS",
    "REPORT_SHEETS",
    "OutputPaths",
    "build_sheets",
    "write_outputs",
]


#: 研报忠实样本止于此；其后为样本外，任何口径选择都不许看这一段。
IN_SAMPLE_END = date(2021, 9, 30)

#: 研报正文口径（年化收益 / Sharpe / 最大回撤 / Calmar / 年化波动）。
PAPER_METRICS: dict[str, float] = {
    "annual_return": 0.1752,
    "sharpe": 1.72,
    "max_drawdown": 0.0827,
    "calmar": 2.12,
    "annual_volatility": 0.1016,
}

_SETTINGS: dict[str, object] = {
    "target_annual_vol": 0.10,
    "cost_bps": 1.3,
    "band_length": 300,
    "band_beta": 1.5,
    "take_profit_std": 8.0,
    "oi_short": 150,
    "oi_long": 300,
    "atr_window": 20,
    "selection_min_trades": 5,
    "selection_observations": 252,
    "realized_vol_observations": 252,
}

_FIDELITY_ROWS: tuple[dict[str, str], ...] = SHARED_FIDELITY_ROWS + (
    {
        "rule_id": "B1",
        "paper_text": "布林通道标准差",
        "implementation": "总体标准差 ddof=0",
        "basis": "研报未披露自由度",
        "status": "preregistered_default",
        "variant": "ddof=1 单独出一份敏感性产物，只用于解释差异",
        "impact": "300 根窗口下两者差异极小，但会改变个别穿越时点",
    },
    {
        "rule_id": "B2",
        "paper_text": "持仓量倍率",
        "implementation": "在开仓信号 bar 冻结，持仓期内不随 OI 变化调整",
        "basis": "研报把倍率写在开仓条件里，未写持仓期内是否重算",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "避免持仓期内因 OI 抖动反复调仓",
    },
    {
        "rule_id": "B3",
        "paper_text": "资金在入选品种间等分",
        "implementation": "按当月全部入选品种等分，无信号品种的份额留作现金",
        "basis": "研报写在入选品种间等分，不是在当日有信号品种间等分",
        "status": "paper_explicit",
        "variant": "none",
        "impact": "组合杠杆低于按活跃品种等分的口径",
    },
    {
        "rule_id": "B4",
        "paper_text": "价格触及止盈线后平仓",
        "implementation": "止盈后必须等待新的通道穿越事件才能再开仓",
        "basis": "研报把开仓写成穿越事件而非通道外状态",
        "status": "paper_explicit",
        "variant": "none",
        "impact": "价格长期停在通道外时不会原地重开",
    },
)

FIDELITY_RULE_IDS: tuple[str, ...] = tuple(
    fidelity_frame(_FIDELITY_ROWS)["rule_id"].tolist()
)


def _fidelity_sheet(sensitivity_only: bool) -> pd.DataFrame:
    rows = []
    for row in _FIDELITY_ROWS:
        copied = dict(row)
        if sensitivity_only and copied["rule_id"] == "B1":
            copied["implementation"] = "总体标准差 ddof=1"
            copied["status"] = "sensitivity_only"
        rows.append(copied)
    return fidelity_frame(rows)


SPEC = ReportSpec(
    title="Guosen Bollinger replication",
    in_sample_end=IN_SAMPLE_END,
    paper_metrics=PAPER_METRICS,
    fidelity=_fidelity_sheet,
    settings=_SETTINGS,
)


def build_sheets(
    result: BacktestResult,
    *,
    universe: pd.DataFrame | None = None,
    dominant_rolls: pd.DataFrame | None = None,
    run_config: Mapping[str, object] | None = None,
    sensitivity_only: bool = False,
) -> dict[str, pd.DataFrame]:
    """Every sheet the Bollinger workbook writes, in the order it writes them."""
    return _build_sheets(
        result,
        spec=SPEC,
        universe=universe,
        dominant_rolls=dominant_rolls,
        run_config=run_config,
        sensitivity_only=sensitivity_only,
    )


def write_outputs(
    result: BacktestResult,
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
