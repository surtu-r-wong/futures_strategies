"""日内与隔夜持仓路径（计划 Task 5）。

本模块把"什么时候进、什么时候减、什么时候出"走完，但**不决定成交价**：
成交价由注入的 `fill_price(bar_index)` 给出。生产上它是分钟层的"信号后 5 分钟
VWAP"（Task 2/7），测试里是确定性字典。这条缝是有意留的 —— 路径逻辑与分钟层的
取数、时段、乘数解析互不依赖，因此可以在分钟层落地之前就被完整验证。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from index_open_momentum.risk import Direction, StopEvent, stop_event
from index_open_momentum.signals import OPENING_BAR_COUNT, OpeningSignal, opening_signal
from index_open_momentum.types import Bar


#: 研报：每次止损减掉初始仓位的三分之一，第三次减完即平。
SCALE_DOWN_STEPS = 3

#: 研报费率 1.3%%（0.3%% 手续费 + 1.0%% 冲击成本），折算成单边收益率 0.00013。
ONE_WAY_COST = 0.00013


class FillKind(StrEnum):
    ENTRY = "entry"
    SCALE_DOWN = "scale_down"
    DAY_CLOSE_EXIT = "day_close_exit"
    OVERNIGHT_EXIT = "overnight_exit"


@dataclass(frozen=True)
class Fill:
    kind: FillKind
    bar_index: int
    price: float
    size: float
    stop: StopEvent | None = None


@dataclass(frozen=True)
class SessionResult:
    direction: Direction | None
    fills: tuple[Fill, ...]
    gross_return: float
    cost: float
    net_return: float
    carried_overnight: float


_DIRECTION_OF = {
    OpeningSignal.LONG: Direction.LONG,
    OpeningSignal.SHORT: Direction.SHORT,
}


def simulate_session(
    *,
    bars: Sequence[Bar | None],
    atr_at: Sequence[float | None],
    fill_price: Callable[[int], float],
    next_session_open: float | None,
) -> SessionResult:
    """走完一个交易日的持仓路径。

    序列里的 ``None`` 表示**这根 15 分钟 K 线没有成交**（`bars.IndexBar.no_trade`）。
    它不更新入场后极值、不触发止损，并且**打断**反向信号的连续计数 —— 见
    `_traded_run_start`。不用平行的布尔数组：那样调用方得伪造一根 `Bar` 才能
    占位，而伪造一根没成交过的 K 线正是 `types.Bar` 反对的事。
    """
    signal = opening_signal(bars)
    direction = _DIRECTION_OF.get(signal)
    if direction is None:
        return SessionResult(None, (), 0.0, 0.0, 0.0, 0.0)

    entry_index = OPENING_BAR_COUNT - 1
    entry_price = fill_price(entry_index)
    fills = [Fill(FillKind.ENTRY, entry_index, entry_price, 1.0)]

    sign = 1.0 if direction is Direction.LONG else -1.0
    step = 1.0 / SCALE_DOWN_STEPS
    remaining = 1.0
    gross = 0.0
    best_high: float | None = None
    best_low: float | None = None

    run_start = 0
    # ⚠️ 复刻假设⑪：**当日最后一根不判减仓**。研报的减仓一律按"信号后 5 分钟
    # VWAP"成交，而最后一根之后没有那个窗口；剩余仓位本来就由日末规则处理
    # （空头收盘平 / 多头留隔夜），所以在最后一根上再减一档是多余的，且只能
    # 拿一个不存在的价去成交。端到端真跑炸出来的：2016-01-11 的 IF 主力在
    # 第 15 根（当日最后一根）触发了吊灯止损。
    for i in range(OPENING_BAR_COUNT, len(bars) - 1):
        bar = bars[i]
        if bar is None:
            # 没成交的这根既没有高低价可计入极值，也没有收盘价可判吊灯；而反向
            # 信号要的是"连续 N 根"，所以它还得把连续计数打断。
            # ⚠️ 复刻假设：研报没写无成交 bar 算不算数。取"打断"而非"透明跳过" ——
            # 透明跳过是在断言"这段时间价格没动过"，比数据支持的更强。
            run_start = i + 1
            continue

        # 入场后的极值从建仓那根之后的第一根开始累计 —— 建仓前的高低价不属于
        # 这笔持仓。⚠️ 复刻假设：研报没写极值从哪根起算。
        best_high = bar.high if best_high is None else max(best_high, bar.high)
        best_low = bar.low if best_low is None else min(best_low, bar.low)

        event = stop_event(
            direction,
            bars[run_start : i + 1],
            atr=atr_at[i],
            best_high_since_entry=best_high,
            best_low_since_entry=best_low,
        )
        if event is None:
            continue

        price = fill_price(i)
        # 研报：每根 bar 最多减一档；第三档减完即平，且当日不再建仓。
        size = remaining if len(_scale_downs(fills)) + 1 == SCALE_DOWN_STEPS else step
        remaining -= size
        gross += sign * (price - entry_price) / entry_price * size
        fills.append(Fill(FillKind.SCALE_DOWN, i, price, size, event))
        if remaining <= 0.0:
            remaining = 0.0
            break

    carried = 0.0
    if remaining > 0.0:
        last = len(bars) - 1
        if direction is Direction.SHORT:
            # 空头当日收盘平，不留隔夜。收盘价必须来自一根**真有成交**的 bar；
            # 一根都没有就硬失败 —— 静默留仓过夜等于把研报的隔夜规则改掉。
            traded_last = _last_traded_index(bars, after=entry_index)
            if traded_last is None:
                raise ValueError(
                    "a short position must be closed on this session's close, but "
                    "no traded bar after entry is available to price it"
                )
            price = bars[traded_last].close
            gross += sign * (price - entry_price) / entry_price * remaining
            fills.append(Fill(FillKind.DAY_CLOSE_EXIT, traded_last, price, remaining))
        else:
            if next_session_open is None:
                raise ValueError(
                    "a long position that survived the session must be closed at the "
                    "next session's open; next_session_open is missing"
                )
            carried = remaining
            gross += sign * (next_session_open - entry_price) / entry_price * remaining
            fills.append(
                Fill(FillKind.OVERNIGHT_EXIT, last, next_session_open, remaining)
            )
        remaining = 0.0

    traded = sum(f.size for f in fills)
    cost = ONE_WAY_COST * traded
    return SessionResult(
        direction=direction,
        fills=tuple(fills),
        gross_return=gross,
        cost=cost,
        net_return=gross - cost,
        carried_overnight=carried,
    )


def _last_traded_index(bars: Sequence[Bar | None], *, after: int) -> int | None:
    """入场之后最后一根有成交的 bar。

    ``after`` 不能省：退到入场那根本身，等于拿**入场成交之前**观测到的收盘价来
    平仓，是回看。没有入场后的成交价就该硬失败，而不是找个近似的顶上。
    """
    for index in range(len(bars) - 1, after, -1):
        if bars[index] is not None:
            return index
    return None


def _scale_downs(fills: Sequence[Fill]) -> list[Fill]:
    return [f for f in fills if f.kind is FillKind.SCALE_DOWN]
