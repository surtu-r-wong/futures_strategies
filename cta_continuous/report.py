"""报告与保真度台账 —— 计划 Task 7。

研报的样本是 2011-06 → **2023-05-31**。本仓已经两次撞上「研报样本恰好止于策略失效
前」（[[guosen-carry-paper-direction-is-wrong]]、[[index-open-momentum-out-of-sample-fails]]），
所以样本外分段在这里是**强制输出**：窗口没走到切点时也留一行说明为什么是空的，
而不是让那一段悄悄消失。缺席的那段指标全给 NaN —— 不给假数，但也不给沉默。

台账（`fidelity_ledger`）是一本**出处账**：研报沉默或自相矛盾的每一处，连同裁定与
依据逐条列出。条数被 `tests/test_continuous_report.py` 钉死，加一条就要在提交里说明
它的来由，否则「研报没写、我们自己定的」那部分会悄悄长大。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import pandas as pd

from common.metrics import cumulative_equity, max_drawdown, summarize
from cta_continuous.backtest import BacktestParams, TurnoverCause

__all__ = [
    "DECISIONS",
    "COST_LEVELS",
    "OUT_OF_SAMPLE_START",
    "PAPER_AVERAGE_LEVERAGE",
    "PAPER_HEADLINE",
    "Decision",
    "Segment",
    "annual_table",
    "build_sheets",
    "cost_sensitivity",
    "fidelity_ledger",
    "leverage_profile",
    "segment_metrics",
    "turnover_breakdown",
    "write_outputs",
]

#: 研报样本的末日是 2023-05-31，所以样本外从次日算起。
OUT_OF_SAMPLE_START = date(2023, 6, 1)

#: 交易日年化基数。
TRADING_DAYS_PER_YEAR = 252

#: 研报 §1.6 最终策略的自报业绩。**不调参逼近它**，只做逐项对账。
PAPER_HEADLINE = {
    "ann_return": 0.231,
    "sharpe": 1.71,
    "calmar": 1.67,
    "ann_vol": 0.1348,
}

#: 研报图 15：平均杠杆 2.5 倍。
PAPER_AVERAGE_LEVERAGE = 2.5

#: 研报表 8 的三档成本。
COST_LEVELS = (1.3, 1.8, 2.5)


@dataclass(frozen=True, slots=True)
class Decision:
    """一条口径裁决：歧义在哪、怎么定的、凭什么。"""

    id: str
    question: str
    ruling: str
    basis: str


#: ⚠️ 顺序即编号，实现与测试都按 `D<n>` 引用。插入要改全套。
DECISIONS: tuple[Decision, ...] = (
    Decision(
        "D1",
        "ATR 算在日线还是 15 分钟线",
        "15 分钟线，n=20",
        "实测四张不同年代的活跃合约：日线 ATR 下 `Lev_ATR>1` 通过率 0.0%，策略永不"
        "开仓；15 分钟 ATR 下 74%~100%、从不触及 4 倍上限",
    ),
    Decision(
        "D2",
        "多头是短均线在上还是长均线在上",
        "短均线在长均线上方为多头",
        "§2.1 推导 + 图 5/7/8 图例；§5.1 汇总框写反。反向由 --ma-orientation reversed 跑对照",
    ),
    Decision(
        "D3",
        "噪音闸是 ΔTNR>0 还是 <0",
        "ΔTNR > 0",
        "§3.1 结论句与表 4 实证（单笔均值 0.29% vs 0.14%）；§5.1 汇总框写反。"
        "反向由 --tnr-sign negative 跑对照",
    ),
    Decision(
        "D4",
        "递推里的 Long/Short 含不含 U2P 闸",
        "不含：只含均线方向 + 距离扩大 + Lev_ATR>1 + ΔTNR 闸",
        "含则自指（U2P 依赖 Long，Long 依赖 U2P），无解。被迫，不是选的",
    ),
    Decision(
        "D5",
        "触发是穿越事件还是状态",
        "状态，逐 bar 判",
        "图 13 有连续两根「再次触发多仓条件」；穿越事件不可能连续两根都发生",
    ),
    Decision(
        "D6",
        "闸门不过时是平仓还是持有到反向",
        "平仓（空仓）",
        "§3.1：「当开仓杠杆率 Lev_ATR<1 …… 即使此时传统信号为 1，我们依然平仓操作」",
    ),
    Decision(
        "D7",
        "ΔTNR 用「与 3 日前比」还是「与近 k 期均值比」",
        "近 k 期均值（公式图），k=3",
        "§3.1 正文说「当日与 3 日前」，与公式图不符；公式图更精确。"
        "正文那一侧由 --dtnr-mode lag 跑对照",
    ),
    Decision(
        "D8",
        "策略起始日",
        "约 2012-01（非研报 2011-06）",
        "商品分钟档案自 2011-01-04 起，Mul_vol 还需 252 个交易日的策略收益垫底",
    ),
    Decision(
        "D9",
        "EMA 跨度、TNR 的 N、ATR 的 n",
        "预注册网格，只在样本内选点；样本外对选中点与全网格都报",
        "研报全部未给",
    ),
    Decision(
        "D10",
        "宇宙刷新节拍",
        "每月末，用截止上一自然月末的 6 个月",
        "研报只说「过去半年」。逐日重算会让品种在 50 亿门槛上抖动、制造纯噪音换手；"
        "研报另一个动件（Mul_vol）明写按月",
    ),
    Decision(
        "D11",
        "主力「双最大」不成立时",
        "不切换，沿用上一主力",
        "研报只给了双最大规则与不可逆约束，没写双最大不成立的情形",
    ),
    Decision(
        "D12",
        "EMA 递推约定",
        "alpha = 2/(span+1)，adjust=False，首项取原值",
        "研报只写「移动平均（EMA）算法」",
    ),
    Decision(
        "D13",
        "无成交 bar 的 TR / TNR",
        "只在有成交的 bar 上取值，无成交 bar 不入 ATR/TNR 窗口，也不推进 U2P 递推",
        "volume=0 的空 K 线带的是结转价不是成交价；让它进窗口会把 TR 压成 0、"
        "把 TNR 的分母压小，两个方向都假装市场比实际更平稳",
    ),
    Decision(
        "D14",
        "信号落在时段最后一根 bar 时的成交窗口",
        "顺延到该品种下一交易时段的前 5 分钟",
        "研报未写。与 index_open_momentum 的 fill_window 姿势一致",
    ),
    Decision(
        "D15",
        "「日均成交额」的分母",
        "窗口内全市场观测到的交易日数，品种缺席那天按 0 计",
        "按品种自己的观测天数取平均，会让刚挂牌、只交易三天但每天 200 亿的品种"
        "被算成「日均 200 亿」入池",
    ),
    Decision(
        "D16",
        "沿用的旧主力已退市 / 从日线池消失",
        "该品种暂不交易，直到新的双最大合约出现；「仍可交易」按 **trade_date 当日**"
        "的合约池判，不是按选择日",
        "主力选择滞后一个交易日，只查选择日的池会让临期合约多活一天：实测六例"
        "（AU1912/AU2012/AU2206/LU2209/FU2309/FU2509）的 selected_from 恰是该合约的"
        "最后一个交易日。到期日是事先公布的日历事实，用它不构成未来函数。"
        "整个品种当日无行时不判退市 —— futures_daily 有已知的整日空洞",
    ),
    Decision(
        "D17",
        "Mul_vol 的已实现波动率从哪条收益序列算",
        "组合层的**影子收益**：只加 Lev_ATR、不乘 Mul_vol",
        "按字面读，第一年 Lev=0 ⇒ 收益恒 0 ⇒ 标准差 0 ⇒ final_leverage 对零波动"
        "硬失败，策略永远启动不了。研报的 Vol 是组合层的量，所以影子也在组合层。"
        "本仓股指侧（index_open_momentum/run.py）先走的也是这条",
    ),
    Decision(
        "D18",
        "展期算不算换手与成本",
        "算，且在四分解里单列；换月当根的信号缩放一并计入 ROLL",
        "后复权连续价让权重看着没动，但旧合约要平、新合约要开。研报通篇没提展期"
        "成本；单列是为了让要 netting 的人自己减。一根 bar 上一张合约只能挂一个"
        "成因，而四类之和必须等于总换手，所以 ROLL 是上界不是纯展期",
    ),
    Decision(
        "D19",
        "成交窗零成交、拿不到成交价时",
        "该笔顺延到该品种下一个有价的槽，并记进 deferred_fills；"
        "掉出宇宙是唯一例外，按最后一次观测到的成交价平掉",
        "拿上一次的成交价当本次的成交价等于凭空造一笔成交，panel.py 已经拒绝过"
        "就地换价。掉出宇宙的品种整月没有行情，等不到下一个有价的槽",
    ),
    Decision(
        "D20",
        "指标预热多少根才算数",
        "max(ema_long, tnr_window + k − 1, atr_window) 根**已成交** bar，按"
        "(品种, 连续段) 各自计",
        "研报没写预热。delta_tnr 要 N+k−1 根才非 NaN，atr_series 窗口未满时取部分"
        "均值，ema 从第一根就有值但那是种子不是均值。EMA 的预热取它自己的跨度 —— "
        "那时种子的残余权重 (1−α)^span ≈ e^{−2} ≈ 13.5%",
    ),
)


# ---------------------------------------------------------------------------
# 分段指标
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Segment:
    name: str
    start: date | None
    end: date | None
    observations: int
    metrics: dict[str, float]
    note: str = ""


_EMPTY_METRICS = {
    "ann_return": float("nan"),
    "ann_vol": float("nan"),
    "sharpe": float("nan"),
    "max_drawdown": float("nan"),
    "win_rate": float("nan"),
    "calmar": float("nan"),
}


def _measure(name: str, returns: pd.Series, note: str = "") -> Segment:
    if returns.empty:
        return Segment(name, None, None, 0, dict(_EMPTY_METRICS), note)
    stats = summarize(returns, periods_per_year=TRADING_DAYS_PER_YEAR)
    drawdown = stats["max_drawdown"]
    stats["calmar"] = (
        stats["ann_return"] / drawdown if drawdown and drawdown > 0 else float("nan")
    )
    index = list(returns.index)
    return Segment(name, index[0], index[-1], len(returns), stats, note)


def segment_metrics(
    daily: pd.DataFrame,
    *,
    column: str = "net_return",
    cut: date = OUT_OF_SAMPLE_START,
) -> tuple[Segment, ...]:
    """全样本 / 样本内 / 样本外，**三行恒在**。

    ⚠️ 缺席的那一段不省略：它给 `observations=0`、指标全 NaN、外加一句 `note` 说明
    为什么是空的。研报样本止于策略失效前这件事本仓撞过两次，一段悄悄消失的样本外
    读者是看不出来的 —— 而那正是最该被看见的一段。
    """
    if daily.empty or column not in daily.columns:
        empty = pd.Series(dtype="float64")
        return (
            _measure("full", empty, "没有任何交易日"),
            _measure("in_sample", empty, "没有任何交易日"),
            _measure("out_of_sample", empty, "没有任何交易日"),
        )

    frame = daily.dropna(subset=[column])
    returns = pd.Series(frame[column].to_numpy(), index=list(frame["trade_date"]))
    inside = returns[[day < cut for day in returns.index]]
    outside = returns[[day >= cut for day in returns.index]]
    return (
        _measure("full", returns),
        _measure(
            "in_sample",
            inside,
            "" if not inside.empty else f"窗口整段落在 {cut} 之后",
        ),
        _measure(
            "out_of_sample",
            outside,
            "" if not outside.empty else f"窗口整段落在 {cut} 之前，样本外无数据",
        ),
    )


# ---------------------------------------------------------------------------
# 分年度表（研报表 7）
# ---------------------------------------------------------------------------

_ANNUAL_COLUMNS = [
    "year",
    "return",
    "max_drawdown",
    "sharpe",
    "ann_vol",
    "calmar",
    "monthly_win_rate",
    "observations",
]


def annual_table(daily: pd.DataFrame, *, column: str = "net_return") -> pd.DataFrame:
    """逐年绩效，列对齐研报表 7。"""
    if daily.empty or column not in daily.columns:
        return pd.DataFrame(columns=_ANNUAL_COLUMNS)

    frame = daily.dropna(subset=[column]).copy()
    frame["year"] = [day.year for day in frame["trade_date"]]
    frame["month"] = [(day.year, day.month) for day in frame["trade_date"]]

    rows = []
    for year, group in frame.groupby("year", sort=True):
        returns = pd.Series(group[column].to_numpy())
        stats = summarize(returns, periods_per_year=TRADING_DAYS_PER_YEAR)
        drawdown = stats["max_drawdown"]
        monthly = group.groupby("month")[column].apply(
            lambda values: float(cumulative_equity(pd.Series(values.to_numpy())).iloc[-1] - 1.0)
        )
        rows.append(
            {
                "year": int(year),
                "return": float(cumulative_equity(returns).iloc[-1] - 1.0),
                "max_drawdown": drawdown,
                "sharpe": stats["sharpe"],
                "ann_vol": stats["ann_vol"],
                "calmar": (
                    stats["ann_return"] / drawdown
                    if drawdown and drawdown > 0
                    else float("nan")
                ),
                "monthly_win_rate": float((monthly > 0).mean()) if len(monthly) else float("nan"),
                "observations": int(len(group)),
            }
        )
    return pd.DataFrame(rows, columns=_ANNUAL_COLUMNS)


# ---------------------------------------------------------------------------
# 台账 / 换手 / 杠杆 / 成本
# ---------------------------------------------------------------------------


def fidelity_ledger() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": decision.id,
                "question": decision.question,
                "ruling": decision.ruling,
                "basis": decision.basis,
            }
            for decision in DECISIONS
        ],
        columns=["id", "question", "ruling", "basis"],
    )


def turnover_breakdown(result) -> pd.DataFrame:
    """换手四分解与各自的成本贡献。

    ⚠️ 四类**每一类都留一行**，哪怕它是 0。隐掉一行，读者就以为那一类不存在 ——
    而「展期从没花过钱」和「展期这次没发生」是两回事。
    """
    executions = getattr(result, "executions", pd.DataFrame())
    totals = {cause.value: (0.0, 0.0) for cause in TurnoverCause}
    if executions is not None and not executions.empty:
        grouped = executions.groupby("cause", observed=True)[["turnover", "cost"]].sum()
        for cause, row in grouped.iterrows():
            totals[str(cause)] = (float(row["turnover"]), float(row["cost"]))

    total_turnover = sum(turnover for turnover, _ in totals.values())
    return pd.DataFrame(
        [
            {
                "cause": cause,
                "turnover": turnover,
                "cost": cost,
                "turnover_share": (turnover / total_turnover) if total_turnover else 0.0,
            }
            for cause, (turnover, cost) in totals.items()
        ],
        columns=["cause", "turnover", "cost", "turnover_share"],
    )


def leverage_profile(daily: pd.DataFrame, *, column: str = "gross_leverage") -> dict:
    """平均杠杆及其月度时序，摆在研报图 15 的「均值 2.5 倍」旁边。"""
    if daily.empty or column not in daily.columns:
        return {
            "mean_gross_leverage": float("nan"),
            "paper_average_leverage": PAPER_AVERAGE_LEVERAGE,
            "monthly": pd.DataFrame(columns=["month", "mean_gross_leverage"]),
        }
    frame = daily.dropna(subset=[column]).copy()
    frame["month"] = [date(day.year, day.month, 1) for day in frame["trade_date"]]
    monthly = (
        frame.groupby("month", sort=True)[column]
        .mean()
        .reset_index()
        .rename(columns={column: "mean_gross_leverage"})
    )
    return {
        "mean_gross_leverage": float(frame[column].mean()),
        "paper_average_leverage": PAPER_AVERAGE_LEVERAGE,
        "monthly": monthly,
    }


_SENSITIVITY_COLUMNS = [
    "cost_bps", "ann_return", "sharpe", "max_drawdown", "ann_vol", "turnover"
]


def cost_sensitivity(
    *,
    runner: Callable[..., object],
    params: BacktestParams,
    levels: Sequence[float] = COST_LEVELS,
) -> pd.DataFrame:
    """研报表 8 的三档成本。

    `runner(params=...)` 由调用方给：生产上是 `run_backtest` 绑好面板与预算好的
    signals（12 次网格共用同一份指标计算），测试里给一个确定性替身。
    """
    rows = []
    for level in levels:
        result = runner(params=replace(params, cost_bps=level))
        daily = getattr(result, "daily", pd.DataFrame())
        full = segment_metrics(daily)[0]
        rows.append(
            {
                "cost_bps": level,
                "ann_return": full.metrics["ann_return"],
                "sharpe": full.metrics["sharpe"],
                "max_drawdown": full.metrics["max_drawdown"],
                "ann_vol": full.metrics["ann_vol"],
                "turnover": (
                    float(daily["turnover"].sum())
                    if not daily.empty and "turnover" in daily
                    else float("nan")
                ),
            }
        )
    return pd.DataFrame(rows, columns=_SENSITIVITY_COLUMNS)


# ---------------------------------------------------------------------------
# 汇总与落盘
# ---------------------------------------------------------------------------


def build_sheets(result) -> dict[str, pd.DataFrame]:
    daily = getattr(result, "daily", pd.DataFrame())
    profile = leverage_profile(daily)
    segments = pd.DataFrame(
        [
            {
                "segment": segment.name,
                "start": segment.start,
                "end": segment.end,
                "observations": segment.observations,
                "note": segment.note,
                **segment.metrics,
            }
            for segment in segment_metrics(daily)
        ]
    )
    leverage = profile["monthly"].copy()
    leverage["paper_average_leverage"] = PAPER_AVERAGE_LEVERAGE
    return {
        "segments": segments,
        "annual": annual_table(daily),
        "fidelity": fidelity_ledger(),
        "turnover": turnover_breakdown(result),
        "leverage": leverage,
        "daily": daily,
    }


def write_outputs(sheets: dict[str, pd.DataFrame], *, prefix: Path) -> dict[str, Path]:
    """一份工作簿 + 一份审计 JSON。

    ⚠️ xlsx **比不了字节**：openpyxl 把写入时刻塞进 `docProps/core.xml`，同一份数字
    连跑两次字节也不同。要做逐点不变的对照就按内容比（逐 sheet 读成 DataFrame）。
    """
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    workbook = prefix.with_suffix(".xlsx")
    with pd.ExcelWriter(workbook) as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name[:31], index=False)

    audit = prefix.parent / f"{prefix.name}-audit.json"
    audit.write_text(
        json.dumps(
            {
                "paper_headline": PAPER_HEADLINE,
                "paper_average_leverage": PAPER_AVERAGE_LEVERAGE,
                "out_of_sample_start": str(OUT_OF_SAMPLE_START),
                "cost_levels": list(COST_LEVELS),
                "decisions": [decision.id for decision in DECISIONS],
                "sheets": {name: len(frame) for name, frame in sheets.items()},
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {"workbook": workbook, "audit": audit}
