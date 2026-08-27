"""涨跌概率递推与开仓闸 —— 计划 Task 5。

研报 §3.2 把「开仓后信号持续度」写成一个二状态概率递推。正文对它一个字都没描述，
公式只在图里：

    UpProb_t   = UpProb_{t−1}   + 0.5 × (DownProb_{t−1} × Long_t − UpProb_{t−1}   × Short_t)
    DownProb_t = DownProb_{t−1} + 0.5 × (UpProb_{t−1}   × Short_t − DownProb_{t−1} × Long_t)

起点 0.5 / 0.5。直觉是：每次同向触发把对侧概率**折半**搬到本侧，所以概率单调逼近
0/1 但永不到达。研报图 13 印出了四步的数值，`tests/test_continuous_signals.py` 拿它
当硬验收。

## ⚠️ D4：递推的输入闸里不含 U2P

`Long_t` / `Short_t` 只含均线方向 + 距离扩大 + `Lev_ATR > 1` + ΔTNR 闸。把 `U2P > 0.2`
也算进去就自指了（U2P 依赖 Long，Long 依赖 U2P），无解。这是被迫的裁定，不是选的。

## ⚠️ D5：触发是「状态」不是「事件」

图 13 有连续两根「再次触发多仓条件」；穿越事件不可能连续两根都发生。所以逐 bar 判
状态（短均线是否在长均线上方），不是判穿越那一刻。

## ⚠️ D2 / D3：研报自相矛盾的两处

§5.1 汇总框写「多头＝长均线位于短均线上方」「ΔTNR < 0」，与 §2.1 的推导、§3.1 的
结论句和表 4 的实证**都相反**。默认取正文一侧，两个开关把汇总框那一侧也留出来跑对照。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "ATR_LEVERAGE_FLOOR",
    "Direction",
    "ProbabilityPath",
    "Signal",
    "U2P_THRESHOLD",
    "gate_flags",
    "position_signal",
    "up_down_prob",
]

#: 研报 §3.2：涨跌概率差的绝对值**大于** 0.2 才认为趋势清晰。
U2P_THRESHOLD = 0.2

#: 研报 §5.1：`Lev_ATR > 1`，即 15 分钟 ATR 不到收盘价的 0.5%。
ATR_LEVERAGE_FLOOR = 1.0

_INITIAL_PROBABILITY = 0.5
_TRANSFER = 0.5


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class ProbabilityPath:
    """逐 bar 的上涨/下跌概率与二者之差。"""

    up: tuple[float, ...]
    down: tuple[float, ...]
    u2p: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Signal:
    """某一根 bar 上的方向与连续信号值。

    `value` 是研报 §5.1「信号调整」的那个量：多头取上涨概率，空头取下跌概率并带负号
    表方向。因 `|U2P| > 0.2` 且两概率和为 1，非空仓时 `|value| > 0.6` 恒成立。
    """

    direction: Direction
    value: float


def up_down_prob(
    *, long_flags: Sequence[bool], short_flags: Sequence[bool]
) -> ProbabilityPath:
    """研报 §3.2 的递推。两个序列必须等长且**不得同一根同时为真**。"""
    if len(long_flags) != len(short_flags):
        raise ValueError("prob_length: 多空触发序列长度必须相同")

    up = _INITIAL_PROBABILITY
    down = _INITIAL_PROBABILITY
    ups: list[float] = []
    downs: list[float] = []
    for index, (is_long, is_short) in enumerate(zip(long_flags, short_flags)):
        if is_long and is_short:
            raise ValueError(
                f"prob_both_sides: 第 {index} 根同时触发多头与空头；"
                "这是上游闸算错了，不是一种可以求和的状态"
            )
        if is_long:
            moved = _TRANSFER * down
            up, down = up + moved, down - moved
        elif is_short:
            moved = _TRANSFER * up
            up, down = up - moved, down + moved
        ups.append(up)
        downs.append(down)
    return ProbabilityPath(
        up=tuple(ups),
        down=tuple(downs),
        u2p=tuple(u - d for u, d in zip(ups, downs)),
    )


def gate_flags(
    *,
    short_above_long: bool,
    widening: bool,
    atr_leverage: float,
    delta_tnr: float,
    tnr_sign: str = "positive",
    ma_orientation: str = "paper",
) -> tuple[bool, bool]:
    """递推的输入闸 `(Long_t, Short_t)` —— **不含 U2P**（D4）。"""
    if not widening or not (atr_leverage > ATR_LEVERAGE_FLOOR):
        return False, False
    if not _noise_gate(delta_tnr, tnr_sign):
        return False, False
    bullish = short_above_long if ma_orientation == "paper" else not short_above_long
    return bullish, not bullish


def _noise_gate(delta_tnr: float, tnr_sign: str) -> bool:
    if tnr_sign == "positive":
        return delta_tnr > 0.0
    if tnr_sign == "negative":
        return delta_tnr < 0.0
    raise ValueError(
        f"tnr_sign: 只接受 'positive'（正文与表 4）或 'negative'（§5.1 汇总框）；got {tnr_sign!r}"
    )


def position_signal(
    *,
    short_above_long: bool,
    widening: bool,
    atr_leverage: float,
    delta_tnr: float,
    u2p: float,
    tnr_sign: str = "positive",
    ma_orientation: str = "paper",
) -> Signal:
    """四道闸全过才建仓；任一不过即**空仓**（D6），不是持有到反向信号。"""
    if ma_orientation not in ("paper", "reversed"):
        raise ValueError(
            "ma_orientation: 只接受 'paper'（§2.1 正文）或 'reversed'（§5.1 汇总框）；"
            f"got {ma_orientation!r}"
        )
    is_long, is_short = gate_flags(
        short_above_long=short_above_long,
        widening=widening,
        atr_leverage=atr_leverage,
        delta_tnr=delta_tnr,
        tnr_sign=tnr_sign,
        ma_orientation=ma_orientation,
    )
    if is_long and u2p > U2P_THRESHOLD:
        return Signal(Direction.LONG, (1.0 + u2p) / 2.0)
    if is_short and u2p < -U2P_THRESHOLD:
        return Signal(Direction.SHORT, -(1.0 - u2p) / 2.0)
    return Signal(Direction.FLAT, 0.0)
