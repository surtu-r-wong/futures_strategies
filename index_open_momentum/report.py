"""报告与保真度披露 —— 计划 Task 8。

## 为什么样本外必须单独成段

研报日期 2021-05-13、样本自 2011-06 起 ⇒ **其样本止于 2021**。本仓已经在国信 Carry
那篇上撞过一次：**研报样本恰好止于策略失效之前**（七个不重叠窗口里四个为负，
2021 后连续三个为负且逐段恶化）。同一家、同一类研报，先验上应当假设这里也一样。

所以「只给全样本合并数」不是风格问题，是**验收失败**。`segment_metrics` 把区间切成
`full` / `in_sample` / `out_of_sample` 三段并列输出，`build_sheets` 的指标表逐段一行 ——
让"只报一个数"这件事在结构上做不到。

## 落盘三件

`<prefix>.xlsx`（七张表）/ `<prefix>.png`（净值图）/ `<prefix>.audit.json`
（`EXPLAIN` 产物 + 乘数出处 + 缺口披露）。

⚠️ **`EXPLAIN` 产物必须落盘**，这是计划 Task 7 第 5 步：留在内存里的计划摘要
在验收时等于不存在。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from common.metrics import cumulative_equity, summarize
from index_open_momentum.run import RunResult, known_gap_disclosure
from index_open_momentum.sessions import CoverageVerdict, coverage_gate

__all__ = [
    "FidelityVerdict",
    "OUT_OF_SAMPLE_START",
    "fidelity_verdict",
    "REPORT_SHEETS",
    "Segment",
    "TRADING_DAYS_PER_YEAR",
    "build_sheets",
    "segment_metrics",
    "write_outputs",
]

#: 研报日期。其样本止于此 ⇒ 之后的每一天都是样本外。
OUT_OF_SAMPLE_START = date(2021, 5, 13)

#: 日频收益的年化基数。`common.metrics` 默认是月频的 12，这里必须显式给。
TRADING_DAYS_PER_YEAR = 252

REPORT_SHEETS = (
    "metrics",
    "daily_returns",
    "annual_returns",
    "trades",
    "signals",
    "leverage",
    "data_quality",
)

#: 研报头条口径，供逐项对账；**不调参逼近它**。
PAPER_HEADLINE = {
    "ann_return": 0.2579,
    "sharpe": 1.77,
    "max_drawdown": 0.0766,
    "calmar": 3.37,
}


@dataclass(frozen=True)
class FidelityVerdict:
    """能不能把这次运行标成 paper-faithful，以及不能的每一条理由。"""

    paper_faithful: bool
    reasons: tuple[str, ...]
    coverage: CoverageVerdict


def fidelity_verdict(
    result: RunResult,
    *,
    calendar_dates,
    window_start: date,
    window_end: date,
    window_allows_paper_faithful: bool,
    max_missing_days: int = 0,
) -> FidelityVerdict:
    """三道否决合成一个判定，**三条理由都报**。

    ⚠️ 这个函数是自查时补的：`sessions.coverage_gate` 在 Task 1 就写好并变异验证过，
    但直到 Task 8 收尾它**从没被任何地方调用**。写好、测好、却没接线的闸等于不存在，
    而且比不存在更糟 —— 它会让人以为这道检查已经在跑。

    只报第一条理由会让人"修一次跑一次"，所以三条一起给。
    """
    observed: dict[str, list[date]] = {}
    for day in result.product_days:
        observed.setdefault(day.product, []).append(day.trade_date)

    coverage = coverage_gate(
        calendar_dates=calendar_dates,
        observed_dates=observed,
        window_start=window_start,
        window_end=window_end,
        max_missing_days=max_missing_days,
    )

    reasons: list[str] = []
    if not window_allows_paper_faithful:
        reasons.append(
            "window: 区间覆盖已登记的档案缺口或含研报之外的品种，结构上标不了"
        )
    if not coverage.paper_faithful:
        worst = max((s.missing for s in coverage.shortfalls), default=0)
        reasons.append(
            f"coverage: 逐年交易日对账有缺口，最多一年缺 {worst} 天"
            f"（阈值 {max_missing_days}）"
        )
    if result.unpriceable_sessions:
        reasons.append(
            f"unpriceable: {result.unpriceable_sessions} 个交易日的必需成交窗零成交，"
            "研报的成交规则无从适用"
        )
    unvalidated = sorted(
        symbol
        for symbol, resolution in result.multipliers.items()
        if resolution.source == "metadata_unvalidated"
    )
    if unvalidated:
        reasons.append(f"multiplier: {unvalidated} 的乘数走了元数据兜底，未过价域校验")

    return FidelityVerdict(
        paper_faithful=not reasons,
        reasons=tuple(reasons),
        coverage=coverage,
    )


@dataclass(frozen=True)
class Segment:
    name: str
    start: date
    end: date
    observations: int
    periods_per_year: int
    metrics: dict[str, float]


def _segment(name: str, returns: pd.Series) -> Segment:
    stats = summarize(returns, periods_per_year=TRADING_DAYS_PER_YEAR)
    drawdown = stats["max_drawdown"]
    stats["calmar"] = (
        stats["ann_return"] / drawdown if drawdown and drawdown > 0 else float("nan")
    )
    index = list(returns.index)
    return Segment(
        name=name,
        start=index[0],
        end=index[-1],
        observations=len(returns),
        periods_per_year=TRADING_DAYS_PER_YEAR,
        metrics=stats,
    )


def segment_metrics(
    daily: pd.DataFrame,
    *,
    column: str = "portfolio",
    cut: date = OUT_OF_SAMPLE_START,
) -> tuple[Segment, ...]:
    """全样本 / 复刻区间 / 样本外，三段并列。

    区间整段落在切点一侧时，**缺席的那段就不出现** —— 给一段空的假数比不给更糟。
    """
    if daily.empty or column not in daily.columns:
        return ()
    returns = daily[column].dropna()
    if returns.empty:
        return ()

    segments = [_segment("full", returns)]
    inside = returns[[d < cut for d in returns.index]]
    outside = returns[[d >= cut for d in returns.index]]
    if not inside.empty:
        segments.append(_segment("in_sample", inside))
    if not outside.empty:
        segments.append(_segment("out_of_sample", outside))
    return tuple(segments)


def _metrics_sheet(daily: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for segment in segment_metrics(daily):
        rows.append(
            {
                "segment": segment.name,
                "start": segment.start,
                "end": segment.end,
                "observations": segment.observations,
                **segment.metrics,
            }
        )
    return pd.DataFrame(rows)


def _annual_sheet(daily: pd.DataFrame, column: str = "portfolio") -> pd.DataFrame:
    if daily.empty or column not in daily.columns:
        return pd.DataFrame(columns=["year", "return", "observations"])
    frame = daily[[column]].copy()
    frame["year"] = [d.year for d in frame.index]
    rows = []
    for year, group in frame.groupby("year", sort=True):
        equity = cumulative_equity(group[column])
        rows.append(
            {
                "year": int(year),
                "return": float(equity.iloc[-1] - 1.0),
                "observations": len(group),
            }
        )
    return pd.DataFrame(rows)


def _product_day_frame(result: RunResult) -> pd.DataFrame:
    if not result.product_days:
        return pd.DataFrame()
    return pd.DataFrame([vars(day) for day in result.product_days])


def _data_quality_sheet(
    result: RunResult, fidelity: FidelityVerdict | None = None
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    if fidelity is not None:
        rows.append(
            {
                "kind": "fidelity",
                "subject": "paper_faithful",
                "detail": (
                    f"{fidelity.paper_faithful}"
                    + (
                        "；否决理由：" + "；".join(fidelity.reasons)
                        if fidelity.reasons
                        else ""
                    )
                ),
            }
        )

    days = _product_day_frame(result)
    if not days.empty:
        start = days["trade_date"].min()
        end = days["trade_date"].max()
        for reason in known_gap_disclosure(start, end):
            rows.append(
                {
                    "kind": "archive_gap",
                    "subject": "cffex_pre2016_close_tail",
                    "detail": reason,
                }
            )
        rows.append(
            {
                "kind": "coverage",
                "subject": "sessions",
                "detail": (
                    f"{start}~{end}；product-day {len(days)}；"
                    f"无成交 bar {int(days['no_trade_bars'].sum())}；"
                    f"缺槽 {int(days['missing_slots'].sum())}"
                ),
            }
        )
        rows.append(
            {
                "kind": "execution",
                "subject": "unpriceable_sessions",
                "detail": (
                    f"{result.unpriceable_sessions} 个"
                    f"（占 {100 * result.unpriceable_sessions / len(days):.2f}%）"
                    "：必需的 5 分钟成交窗零成交，当日不建仓"
                ),
            }
        )
        rows.append(
            {
                "kind": "execution",
                "subject": "vwap_excursion",
                "detail": (
                    f"最大相对越界 {float(days['max_relative_excursion'].max()):.3e}"
                ),
            }
        )

    for symbol, resolution in sorted(result.multipliers.items()):
        rows.append(
            {
                "kind": "multiplier",
                "subject": symbol,
                "detail": (
                    f"{resolution.multiplier} source={resolution.source} "
                    f"sample_dates={resolution.sample_dates} "
                    f"pass_rate={resolution.pass_rate}"
                ),
            }
        )

    rows.append(
        {
            "kind": "plan",
            "subject": "explain_summaries",
            "detail": f"{len(result.plan_summaries)} 条，已随 .audit.json 落盘",
        }
    )
    return pd.DataFrame(rows, columns=["kind", "subject", "detail"])


def build_sheets(
    result: RunResult, *, fidelity: FidelityVerdict | None = None
) -> dict[str, pd.DataFrame]:
    """七张表。缺数据时给空表而不是缺表 —— 缺表会让下游以为"没这回事"。"""
    days = _product_day_frame(result)
    traded = days.loc[days["direction"].notna()] if not days.empty else pd.DataFrame()
    return {
        "metrics": _metrics_sheet(result.daily),
        "daily_returns": result.daily.reset_index()
        if not result.daily.empty
        else pd.DataFrame(),
        "annual_returns": _annual_sheet(result.daily),
        "trades": traded,
        "signals": days,
        "leverage": (
            days[
                [
                    "trade_date",
                    "product",
                    "leverage",
                    "realized_vol",
                    "atr_at_entry",
                ]
            ]
            if not days.empty
            else pd.DataFrame()
        ),
        "data_quality": _data_quality_sheet(result, fidelity),
    }


def _write_chart(daily: pd.DataFrame, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(figsize=(10, 4))
    if not daily.empty and "portfolio" in daily.columns:
        equity = cumulative_equity(daily["portfolio"])
        axes.plot(list(equity.index), list(equity.to_numpy()), linewidth=1.0)
        # 样本外那一刀画出来 —— 图上也不许把两段混成一条无标记的线。
        if any(d >= OUT_OF_SAMPLE_START for d in daily.index):
            axes.axvline(OUT_OF_SAMPLE_START, linestyle="--", linewidth=0.8)
            axes.annotate(
                f"研报日期 {OUT_OF_SAMPLE_START}（其后为样本外）",
                xy=(OUT_OF_SAMPLE_START, axes.get_ylim()[1]),
                fontsize=8,
                va="top",
            )
    else:
        axes.text(0.5, 0.5, "no sessions", ha="center", va="center")
    axes.set_title("index open momentum net value")
    figure.tight_layout()
    figure.savefig(path, dpi=120)
    plt.close(figure)


def _audit_payload(result: RunResult) -> dict[str, Any]:
    return {
        "plans": [
            plan if isinstance(plan, dict) else _plan_dict(plan)
            for plan in result.plan_summaries
        ],
        "multipliers": {
            symbol: {
                "multiplier": resolution.multiplier,
                "source": resolution.source,
                "sample_dates": resolution.sample_dates,
                "sample_rows": resolution.sample_rows,
            }
            for symbol, resolution in sorted(result.multipliers.items())
        },
        "paper_headline": PAPER_HEADLINE,
        "out_of_sample_start": OUT_OF_SAMPLE_START.isoformat(),
        "segments": [
            {
                "name": segment.name,
                "start": segment.start.isoformat(),
                "end": segment.end.isoformat(),
                "observations": segment.observations,
                "metrics": segment.metrics,
            }
            for segment in segment_metrics(result.daily)
        ],
    }


def _plan_dict(plan: Any) -> dict[str, Any]:
    return {
        field: getattr(plan, field)
        for field in (
            "query_kind",
            "lower_bound",
            "upper_bound",
            "candidate_contract_days",
            "referenced_chunks",
            "maximum_plan_rows",
            "node_types",
        )
        if hasattr(plan, field)
    }


def write_outputs(
    result: RunResult,
    *,
    output_prefix: str,
    fidelity: FidelityVerdict | None = None,
) -> tuple[Path, ...]:
    """落盘三件：工作簿、净值图、审计 JSON。

    ⚠️ 审计 JSON 里的 `plans` 就是计划 Task 7 第 5 步要的 `EXPLAIN` 产物存档 ——
    留在内存里的计划摘要在验收时等于不存在。
    """
    prefix = Path(output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)

    workbook = prefix.with_suffix(".xlsx")
    chart = prefix.with_suffix(".png")
    audit = prefix.with_suffix(".audit.json")

    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        for name in REPORT_SHEETS:
            sheet = build_sheets(result, fidelity=fidelity)[name]
            (sheet if not sheet.empty else pd.DataFrame({"empty": [True]})).to_excel(
                writer, sheet_name=name, index=False
            )
    _write_chart(result.daily, chart)
    audit.write_text(
        json.dumps(_audit_payload(result), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return (workbook, chart, audit)
