"""两条商品复刻命令行共用的窗口判定、覆盖闸与产物切片。

**能调的只有研报没披露的量。** 通道长度、EMA 周期、目标波动、样本截止这些都写死
在各自策略里，不做开关 —— `metrics` 表里明摆着复刻与研报的差距，任何一个被做成
命令行参数的口径，都会变成把那个差距拧到零的旋钮，而那不是复刻。

**区间超出规则资产就拒绝，不外推。** 交易时段规则是版本化资产，它的两端就是这条
线能服务的两端。
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, NoReturn

import pandas as pd

from common.commodity.backtest import BacktestResult
from common.commodity.bundle import PanelBundle
from common.minute.sessions import load_session_rules

__all__ = [
    "SESSION_RULES_LAST",
    "SESSION_RULES_PATH",
    "SESSION_RULES_START",
    "check_coverage",
    "check_window",
    "fail",
    "restrict_result",
    "slice_bundle",
]


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
#: 窗口闸读的必须是**面板据以构建的**那份资产：Bollinger 与道氏的面板出自商品复刻
#: 自己的采集（2012-01-04 起、窗口内曾入池的 63 个品种）。指着 Carry 那份会放行一段
#: 面板根本没有的区间（2011 年），把「资产服务不了」推迟成「bundle 覆盖不了」。
SESSION_RULES_PATH = _REPO_ROOT / "config" / "commodity_minute_sessions.csv"


@lru_cache(maxsize=1)
def _session_bounds() -> tuple[date, date]:
    rules = load_session_rules(SESSION_RULES_PATH)
    if not rules:
        raise SystemExit("commodity_cli: 交易时段规则资产为空")
    ends = [rule.effective_end for rule in rules]
    if any(end is None for end in ends):
        raise SystemExit("commodity_cli: 交易时段规则含开放上界，无法判定可服务区间")
    return min(rule.effective_start for rule in rules), max(
        end for end in ends if end is not None
    )


SESSION_RULES_START, SESSION_RULES_LAST = _session_bounds()


def fail(prog: str, message: str) -> NoReturn:
    raise SystemExit(f"{prog}: {message}")


def check_window(prog: str, start: date, end: date) -> None:
    """Refuse a window the versioned session-rule asset cannot serve."""
    if end < start:
        fail(prog, f"--end {end} 早于 --start {start}")
    if start < SESSION_RULES_START:
        fail(
            prog,
            f"--start {start} 早于交易时段规则资产起点 {SESSION_RULES_START}；"
            "补规则前不许外推",
        )
    if end > SESSION_RULES_LAST:
        fail(
            prog,
            f"--end {end} 超出交易时段规则资产上界 {SESSION_RULES_LAST}；"
            "补规则前不许外推",
        )


def check_coverage(
    prog: str,
    bundle: PanelBundle,
    *,
    start: date,
    end: date,
    require_paper_faithful: bool,
) -> None:
    """Refuse a bundle that does not cover the request, before any work starts."""
    days = pd.to_datetime(bundle.bars["trade_date"])
    if days.empty:
        fail(prog, "面板 coverage 为空")
    first, last = days.min().date(), days.max().date()
    if last < end or first > start:
        fail(prog, f"面板 coverage {first}..{last} 不含请求区间 {start}..{end}")
    if require_paper_faithful:
        window = bundle.bars.loc[(days.dt.date >= start) & (days.dt.date <= end)]
        unpriceable = int(window["fill_unpriceable"].sum())
        if unpriceable:
            fail(prog, f"忠实模式：区间内有 {unpriceable} 根应成交窗口无法定价")


def slice_bundle(bundle: PanelBundle, end: date) -> PanelBundle:
    """Keep every bar up to ``end`` -- warmup lives before ``start``, not after."""
    bars = bundle.bars.loc[pd.to_datetime(bundle.bars["trade_date"]).dt.date <= end]
    rolls = bundle.roll_fills
    if not rolls.empty:
        rolls = rolls.loc[pd.to_datetime(rolls["trade_date"]).dt.date <= end]
    return PanelBundle(
        bars=bars.reset_index(drop=True),
        universes=bundle.universes,
        dominants=bundle.dominants,
        roll_fills=rolls.reset_index(drop=True),
        manifest=bundle.manifest,
    )


def restrict_result(result: BacktestResult, start: date) -> BacktestResult:
    """Report from ``start``; everything earlier was warmup, not performance."""

    def cut(frame: pd.DataFrame, column: str) -> pd.DataFrame:
        if frame.empty or column not in frame.columns:
            return frame
        keep = pd.to_datetime(frame[column]).dt.date >= start
        return frame.loc[keep].reset_index(drop=True)

    return BacktestResult(
        daily=cut(result.daily, "trade_date"),
        positions=cut(result.positions, "trade_date"),
        trades=cut(result.trades, "trade_date"),
        signals=cut(result.signals, "trade_date"),
        selection=cut(result.selection, "month_start"),
        data_quality=result.data_quality,
    )
