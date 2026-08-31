"""工作簿、净值图与审计档案 —— 两篇研报复刻共用的报告层。

两条规则决定这一层长什么样，且对两篇一视同仁。

**研报只测过它自己的样本。** 所以研报口径只写在 `in_sample` 那一行，任何后面的
区间都不许借它的数字；样本内外的分界线还要画在图上，不许把两段混成一条无标记的线。

**差距是产出，不是靶子。** `metrics` 表里 `gap_*` 列摆在那儿是给人读的。真要把它
拧到零，能拧的只有研报从没披露的那几个量 —— 那不是复刻，是拟合。所以敏感性变体
永远另出一份产物并标 `sensitivity_only`，而不是一个可以被"提拔"成默认的选项。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import numpy as np
import pandas as pd

from common.commodity.reporting import excel_safe_frame, split_metrics
from common.metrics import cumulative_equity

__all__ = [
    "REPORT_SHEETS",
    "SHARED_FIDELITY_ROWS",
    "OutputPaths",
    "ReportSpec",
    "build_sheets",
    "write_outputs",
]


#: 设计文档 §12 里标"两者"的保真度裁决 —— 两篇研报共用同一份口径与措辞。
SHARED_FIDELITY_ROWS: tuple[dict[str, str], ...] = (

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
        "rule_id": "F10",
        "paper_text": "主力连续价格序列",
        "implementation": (
            "新旧主力有效收盘区间严格不相交、且主力链在此处断过（新主力不是从旧主力"
            "最后一天接手）时，开启新连续段：复权因子重置 1.0，指标与状态机按段重新"
            "预热，段末最后一根强制平仓"
        ),
        "basis": (
            "燃料油 2018 年摘牌重挂（FU1804 末日 03-30、FU1901 首日 07-16）没有共同"
            "收盘日，不存在可观察的复权比率；全历史仅此一处。判据不能用「旧合约让出"
            "主力即停牌」——归档给已挂牌未成交的合约照打结算价，老 FU 合约零成交打到"
            "2018-06-28，按那条判据这唯一一处真断代反而会被判成缺数据"
        ),
        "status": "preregistered_default",
        "variant": "none",
        "impact": "断代两侧不共享任何价格状态；退市合约不会被带过断层",
    },
    {
        "rule_id": "F9",
        "paper_text": "过去一年策略已实现波动率",
        "implementation": "窗口内收益全为零时视为预热未完成：本月不建仓，并计入 data_quality",
        "basis": "研报未写预热期怎么处理；全零窗口是'还没开始交易'，不是'波动率为零'",
        "status": "preregistered_default",
        "variant": "none",
        "impact": "样本开头若干月不建仓，而不是让整段回测硬失败",
    },
    {
        "rule_id": "F8",
        "paper_text": "主力合约切换",
        "implementation": (
            "在新交易日首个可成交窗口平旧腿、建新腿，两腿分别计成本；某一腿在该窗口"
            "零成交时不发成交单（全历史 3,406 次换月里 96 次：旧合约已到期，或那五"
            "分钟没人交易），连续价仍由日线收盘算出的复权因子缝合"
        ),
        "basis": (
            "研报未写换月的成交时点与成本口径；也没有人能在一个没有成交的窗口里完成"
            "这次转移，而合成一个价会把不存在的成交写成事实"
        ),
        "status": "known_degradation",
        "variant": "none",
        "impact": (
            "换月成本进入交易归因的 roll_old / roll_new；那 96 次切换的价差成本按 0 记，"
            "清单见 bundle 目录的 roll-fill-unpriceable.csv"
        ),
    },
)

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


@dataclass(frozen=True, slots=True)
class OutputPaths:
    """Where one run's three artefacts landed."""

    xlsx: Path
    png: Path
    audit: Path


@dataclass(frozen=True, slots=True)
class ReportSpec:
    """What one replication contributes to an otherwise identical report."""

    title: str
    in_sample_end: date
    paper_metrics: Mapping[str, float]
    fidelity: Callable[[bool], pd.DataFrame]
    settings: Mapping[str, object]


def _empty(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


def _metrics_sheet(daily: pd.DataFrame, spec: ReportSpec) -> pd.DataFrame:
    metrics = split_metrics(
        daily.loc[:, ["trade_date", "net_return"]], in_sample_end=spec.in_sample_end
    )
    for name, value in spec.paper_metrics.items():
        metrics[f"paper_{name}"] = np.where(
            metrics["period"] == "in_sample", value, np.nan
        )
        metrics[f"gap_{name}"] = metrics[name] - metrics[f"paper_{name}"]
    return metrics


def _daily_sheet(daily: pd.DataFrame, spec: ReportSpec) -> pd.DataFrame:
    sheet = daily.copy()
    returns = pd.Series(
        sheet["net_return"].to_numpy(dtype="float64"),
        index=pd.Index(sheet["trade_date"], name="trade_date"),
        dtype="float64",
    )
    equity = cumulative_equity(returns)
    sheet["report_equity"] = equity.to_numpy()
    sheet["drawdown"] = (equity / equity.cummax() - 1.0).to_numpy()
    sheet["sample"] = [
        "in_sample"
        if pd.Timestamp(day).date() <= spec.in_sample_end
        else "out_of_sample"
        for day in sheet["trade_date"]
    ]
    return sheet


def _run_config_sheet(
    spec: ReportSpec, run_config: Mapping[str, object] | None
) -> pd.DataFrame:
    settings: dict[str, object] = {
        "in_sample_end": spec.in_sample_end.isoformat(),
        **dict(spec.settings),
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


def build_sheets(
    result: Any,
    *,
    spec: ReportSpec,
    universe: pd.DataFrame | None = None,
    dominant_rolls: pd.DataFrame | None = None,
    run_config: Mapping[str, object] | None = None,
    sensitivity_only: bool = False,
) -> dict[str, pd.DataFrame]:
    """Every sheet the workbook writes, in the order it writes them."""
    for name in ("daily", "positions", "trades", "signals", "selection", "data_quality"):
        if not isinstance(getattr(result, name, None), pd.DataFrame):
            raise ValueError(f"commodity_report_{name}: expected a DataFrame")
    sheets = {
        "metrics": _metrics_sheet(result.daily, spec),
        "daily_returns": _daily_sheet(result.daily, spec),
        "positions": result.positions,
        "trades": result.trades,
        "signals": result.signals,
        "universe": (
            universe if universe is not None else _empty(("month_start", "product"))
        ),
        "selection": result.selection,
        "dominant_rolls": (
            dominant_rolls
            if dominant_rolls is not None
            else _empty(("trade_date", "product", "old_contract", "new_contract"))
        ),
        "data_quality": result.data_quality,
        "fidelity": spec.fidelity(sensitivity_only),
        "run_config": _run_config_sheet(spec, run_config),
    }
    missing = [name for name in REPORT_SHEETS if name not in sheets]
    if missing:
        raise ValueError(f"commodity_report_sheets: missing={missing!r}")
    return {name: sheets[name] for name in REPORT_SHEETS}


def _write_chart(daily: pd.DataFrame, path: Path, spec: ReportSpec) -> None:
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
        for axis, (label, series) in zip(axes, panels):
            axis.plot(days, list(series.to_numpy()), linewidth=1.0)
            axis.set_ylabel(label)
            axis.axvline(
                pd.Timestamp(spec.in_sample_end), linestyle="--", linewidth=0.8
            )
        axes[0].set_title(f"{spec.title} (paper sample ends {spec.in_sample_end})")
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
    return hashlib.sha256(
        frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    ).hexdigest()


def _audit_payload(
    sheets: Mapping[str, pd.DataFrame],
    *,
    spec: ReportSpec,
    manifest: Mapping[str, object] | None,
    run_config: Mapping[str, object] | None,
    query_plans: Sequence[Mapping[str, object]] | None,
    sensitivity_only: bool,
) -> dict[str, Any]:
    return {
        "commit": _repo_commit(),
        "in_sample_end": spec.in_sample_end.isoformat(),
        "sensitivity_only": sensitivity_only,
        "manifest": dict(manifest) if manifest else {},
        "run_config": dict(run_config) if run_config else {},
        "query_plans": [dict(plan) for plan in (query_plans or ())],
        "paper_metrics": dict(spec.paper_metrics),
        "metrics": sheets["metrics"].to_dict("records"),
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
    result: Any,
    *,
    spec: ReportSpec,
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
        spec=spec,
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
    _write_chart(sheets["daily_returns"], paths.png, spec)
    paths.audit.write_text(
        json.dumps(
            _audit_payload(
                sheets,
                spec=spec,
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
