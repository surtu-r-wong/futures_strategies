"""Workbook, chart, and audit archive for the Bollinger replication.

Two rules shape this module. The paper's own sample stops at 2021-09-30, so
every table that carries a number says which side of that line it is on and the
paper's headline sits only beside the segment the paper actually measured.
And the gap between paper and replication is *reported*, never closed: nothing
here may be tuned until the numbers agree, because the only parameters left to
turn are the ones the paper never disclosed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import numpy as np
import pandas as pd

from common.commodity.reporting import (
    excel_safe_frame,
    fidelity_frame,
    split_metrics,
)
from common.metrics import cumulative_equity
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

REPORT_SHEETS = (
    "metrics",
    "daily_returns",
    "positions",
    "trades",
    "signals",
    "universe",
    "selection",
    "dominant_rolls",
    "data_quality",
    "fidelity",
    "run_config",
)

_FIDELITY_ROWS: tuple[dict[str, str], ...] = (
    {
        "rule_id": "F1",
        "paper_text": "以成交量与持仓量最大的合约为主力合约",
        "implementation": "用前一交易日的量与持仓选当日主力，当日不参与自身选择",
        "basis": "研报未写用哪一日的量仓；用当日即前视",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "换月时点整体后移一个交易日",
    },
    {
        "rule_id": "F2",
        "paper_text": "成交量与持仓量同时最大",
        "implementation": "双最大不成立时沿用上一主力，不切换",
        "basis": "研报未写双最大不成立时怎么办",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "避免在量仓分歧日来回切换合约",
    },
    {
        "rule_id": "F3",
        "paper_text": "每月更新投资标的",
        "implementation": "宇宙与品种筛选均按自然月更新，窗口截到上月末",
        "basis": "研报写按月，未写按日",
        "status": "paper_explicit",
        "variant": "none",
        "impact": "月内标的与杠杆保持不变",
    },
    {
        "rule_id": "F4",
        "paper_text": "过去六个月日均成交额不低于 50 亿元",
        "implementation": "分母取窗口内全市场交易日数，品种缺席日按零成交额",
        "basis": "研报未写分母；用品种自身出现日会高估冷门品种",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "上市初期品种更晚入池",
    },
    {
        "rule_id": "F5",
        "paper_text": "ATR",
        "implementation": "20 根有成交 15 分钟 bar 的简单移动平均",
        "basis": "研报未披露 ATR 周期与频率",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "直接搬动每笔的 ATR 杠杆",
    },
    {
        "rule_id": "F6",
        "paper_text": "过去一年策略已实现波动率",
        "implementation": "最近 252 个完整日收益的样本标准差（ddof=1）年化，按月更新",
        "basis": "研报未写自由度与重算节拍；逐日重算会改变换手",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "月内乘数恒定，观测数不足时不建仓",
    },
    {
        "rule_id": "F7",
        "paper_text": "成交价取信号后五分钟均价",
        "implementation": "郑商所 amount 为合成值，改用 OHLC typical price 并逐笔标记",
        "basis": "郑商所分钟 amount 按单一整数价合成，算不出 VWAP",
        "status": "known_degradation",
        "variant": "none",
        "impact": "郑商所品种成交价不是精确 VWAP，data_quality 逐笔可查",
    },
    {
        "rule_id": "F8",
        "paper_text": "主力合约切换",
        "implementation": "在新交易日首个可成交窗口平旧腿、建新腿，两腿分别计成本",
        "basis": "研报未写换月的成交时点与成本口径",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "换月成本进入交易归因的 roll_old / roll_new",
    },
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


@dataclass(frozen=True, slots=True)
class OutputPaths:
    """Where one run's three artefacts landed."""

    xlsx: Path
    png: Path
    audit: Path


def _empty(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


def _sample_label(day: object) -> str:
    return "in_sample" if pd.Timestamp(day).date() <= IN_SAMPLE_END else "out_of_sample"


def _metrics_sheet(daily: pd.DataFrame) -> pd.DataFrame:
    metrics = split_metrics(
        daily.loc[:, ["trade_date", "net_return"]], in_sample_end=IN_SAMPLE_END
    )
    for name, value in PAPER_METRICS.items():
        metrics[f"paper_{name}"] = np.where(
            metrics["period"] == "in_sample", value, np.nan
        )
        metrics[f"gap_{name}"] = metrics[name] - metrics[f"paper_{name}"]
    return metrics


def _daily_sheet(daily: pd.DataFrame) -> pd.DataFrame:
    sheet = daily.copy()
    returns = pd.Series(
        sheet["net_return"].to_numpy(dtype="float64"),
        index=pd.Index(sheet["trade_date"], name="trade_date"),
        dtype="float64",
    )
    equity = cumulative_equity(returns)
    sheet["report_equity"] = equity.to_numpy()
    sheet["drawdown"] = (equity / equity.cummax() - 1.0).to_numpy()
    sheet["sample"] = [_sample_label(day) for day in sheet["trade_date"]]
    return sheet


def _run_config_sheet(run_config: Mapping[str, object] | None) -> pd.DataFrame:
    settings: dict[str, object] = {
        "in_sample_end": IN_SAMPLE_END.isoformat(),
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
    if run_config:
        settings.update(dict(run_config))
    return pd.DataFrame(
        [
            {"key": key, "value": json.dumps(value, ensure_ascii=False, default=str)}
            for key, value in sorted(settings.items())
        ],
        columns=["key", "value"],
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


def build_sheets(
    result: BacktestResult,
    *,
    universe: pd.DataFrame | None = None,
    dominant_rolls: pd.DataFrame | None = None,
    run_config: Mapping[str, object] | None = None,
    sensitivity_only: bool = False,
) -> dict[str, pd.DataFrame]:
    """Every sheet the workbook writes, in the order it writes them."""
    if not isinstance(result, BacktestResult):
        raise ValueError("bollinger_report_result: expected a BacktestResult")
    sheets = {
        "metrics": _metrics_sheet(result.daily),
        "daily_returns": _daily_sheet(result.daily),
        "positions": result.positions,
        "trades": result.trades,
        "signals": result.signals,
        "universe": (
            universe
            if universe is not None
            else _empty(("month_start", "product"))
        ),
        "selection": result.selection,
        "dominant_rolls": (
            dominant_rolls
            if dominant_rolls is not None
            else _empty(("trade_date", "product", "old_contract", "new_contract"))
        ),
        "data_quality": result.data_quality,
        "fidelity": _fidelity_sheet(sensitivity_only),
        "run_config": _run_config_sheet(run_config),
    }
    missing = [name for name in REPORT_SHEETS if name not in sheets]
    if missing:
        raise ValueError(f"bollinger_report_sheets: missing={missing!r}")
    return {name: sheets[name] for name in REPORT_SHEETS}


def _write_chart(daily: pd.DataFrame, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    if daily.empty:
        axes[0].text(0.5, 0.5, "no sessions", ha="center", va="center")
    else:
        days = list(pd.to_datetime(daily["trade_date"]))
        panels = (
            ("net value", daily["report_equity"]),
            ("drawdown", daily["drawdown"]),
            ("gross leverage", daily["gross_leverage"]),
        )
        for axis, (title, series) in zip(axes, panels):
            axis.plot(days, list(series.to_numpy()), linewidth=1.0)
            axis.set_ylabel(title)
            # 样本内外那一刀必须画出来，不许把两段混成一条无标记的线。
            axis.axvline(pd.Timestamp(IN_SAMPLE_END), linestyle="--", linewidth=0.8)
        axes[0].set_title(
            f"Guosen Bollinger replication (paper sample ends {IN_SAMPLE_END})"
        )
    figure.tight_layout()
    figure.savefig(path, dpi=120)
    plt.close(figure)


def _repo_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def _digest(frame: pd.DataFrame) -> str:
    content = frame.to_csv(index=False, lineterminator="\n")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _audit_payload(
    sheets: Mapping[str, pd.DataFrame],
    *,
    manifest: Mapping[str, object] | None,
    run_config: Mapping[str, object] | None,
    query_plans: Sequence[Mapping[str, object]] | None,
    sensitivity_only: bool,
) -> dict[str, Any]:
    metrics = sheets["metrics"]
    return {
        "commit": _repo_commit(),
        "in_sample_end": IN_SAMPLE_END.isoformat(),
        "sensitivity_only": sensitivity_only,
        "manifest": dict(manifest) if manifest else {},
        "run_config": dict(run_config) if run_config else {},
        "query_plans": [dict(plan) for plan in (query_plans or ())],
        "paper_metrics": dict(PAPER_METRICS),
        "metrics": metrics.to_dict("records"),
        "fidelity_variants": [
            {
                "rule_id": row["rule_id"],
                "status": row["status"],
                "variant": row["variant"],
            }
            for row in sheets["fidelity"].to_dict("records")
        ],
        "sheets": {
            name: {"rows": int(len(frame)), "sha256": _digest(frame)}
            for name, frame in sheets.items()
        },
    }


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
    prefix = Path(output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    paths = OutputPaths(
        xlsx=prefix.with_suffix(".xlsx"),
        png=prefix.with_suffix(".png"),
        audit=prefix.with_suffix(".audit.json"),
    )

    sheets = build_sheets(
        result,
        universe=universe,
        dominant_rolls=dominant_rolls,
        run_config=run_config,
        sensitivity_only=sensitivity_only,
    )
    with pd.ExcelWriter(paths.xlsx, engine="openpyxl") as writer:
        for name in REPORT_SHEETS:
            frame = sheets[name]
            written = (
                excel_safe_frame(frame)
                if not frame.empty
                else pd.DataFrame({"empty": [True]})
            )
            written.to_excel(writer, sheet_name=name, index=False)
    _write_chart(sheets["daily_returns"], paths.png)
    paths.audit.write_text(
        json.dumps(
            _audit_payload(
                sheets,
                manifest=manifest,
                run_config=run_config,
                query_plans=query_plans,
                sensitivity_only=sensitivity_only,
            ),
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return paths
