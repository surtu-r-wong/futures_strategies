"""组合回测 —— 计划 Task 6。

研报 §5.1 的资金口径只有一句话：「满足开仓条件的品种等权分配资金」，权重再乘
`Lev × Signal`，成交价取信号那根 bar 之后 5 分钟的 VWAP，成本 1.3 个基点。附录一
补了跨品种对齐：非交易时段行情置空、**信号延续**、**收益率置零**。

## 两遍

1. `product_signals` —— 逐 `(品种, 连续段)` 算指标与信号。这一层**只吃有成交的
   bar**（D13），空 K 线带的是结转价；算完再铺回全局栅格，空 bar 上延续上一根的
   方向与信号值。
2. `run_backtest` —— 沿全局事件时间推进 `common.minute.account.EventAccount`。

## ⚠️ 事件时刻取的是**成交窗**，不是 bar 收盘

信号在 bar 收盘产生、在其后 5 分钟成交，所以每根 bar 对应一个事件、事件价就是那根
的 `fill_price`。价格链于是是 `fill(1) → fill(2) → …`：每一段都由上一根决定的权重
持有，既没有未来函数，也不会把同一段收益记两遍。

一天最后一根桶没有「之后 5 分钟」，它的成交价来自**下一交易时段的前 5 分钟**
（D14）。所以那一笔的事件时刻与所属交易日都要落到**下一天**，否则隔夜跳空会被记进
前一天，而日收益正是波动率反馈的输入。

## ⚠️ D17：`Mul_vol` 读的是影子收益

`Mul_vol = 15% / Vol`，`Vol` 是「过去一年**策略收益**的已实现波动率」。按字面读，
第一年 `Lev = 0` ⇒ 收益恒 0 ⇒ 标准差 0 ⇒ `common.leverage.final_leverage` 对零
波动硬失败 —— 策略永远启动不了。所以波动率窗口读一条**只加 `Lev_ATR`、不乘
`Mul_vol`** 的影子组合收益。这条不是选的，是被迫的；本仓股指侧
（`index_open_momentum/run.py`）先走的也是它。研报的 `Vol` 是组合层的量，所以影子
也在组合层，不是逐品种。

## ⚠️ D18：展期算换手

后复权连续价让持仓权重在换月那一刻看着没动，但旧合约要平、新合约要开，两笔都是
真实成交。账户按**合约**记权重，所以这两笔自然落成两条 `ExecutionRecord`，本层只
负责把它们标成 `TurnoverCause.ROLL`。研报通篇没提展期成本 —— 报告里单列，要 netting
的人自己减。

⚠️ 换月那一根上信号强度通常也变了，那部分缩放会**一并**算进 ROLL：一根 bar 上一张
合约只能挂一个成因，而四类之和必须等于总换手。所以 ROLL 是「展期及其当根缩放」的
上界，不是纯粹的展期成本。

## ⚠️ D19：拿不到成交价就顺延

`fill_price` 为空有两种来源：成交窗零成交（`fill_unpriceable`），以及全历史最后一
根（`fill_pending`，没有下一时段）。两种都不许就地换一个价 —— `panel.py` 已经明确
拒绝过「换价等于凭空造成交」。本层把那一笔顺延到该品种下一个有价的槽，并记进
`deferred_fills`。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

import math
import numpy as np
import pandas as pd

from common.leverage import (
    TRADING_DAYS_PER_YEAR,
    atr_leverage,
    final_leverage,
    monthly_realized_volatility,
)
from common.minute.account import EventAccount
from cta_continuous.indicators import (
    atr_series,
    delta_tnr,
    ema,
    gap_widening,
    tnr_series,
)
from cta_continuous.signals import (
    EXIT_RULES,
    Direction,
    gate_flags,
    crossover_path,
    position_path,
)

__all__ = [
    "BacktestParams",
    "BacktestResult",
    "TurnoverCause",
    "product_signals",
    "run_backtest",
]

#: 研报 §5.1：手续费 0.3%% + 冲击 1%%。
DEFAULT_COST_BPS = 1.3

#: `fill_price` 的成交窗长度，与 `panel.FILL_MINUTES` 同源。
FILL_MINUTES = 5

#: 15 分钟桶的长度。一天最后一根的成交窗落在下一时段的前 5 分钟，所以它的事件时刻
#: 是「下一根 bar 的 slot_end 减去（15 − 5）分钟」。
BUCKET_MINUTES = 15

#: 调仓节拍（计划 D22）。收窄的是**再平衡**，不是开平仓 —— 信号翻转、掉出宇宙、
#: 展期在任何节拍下都照常成交。
#:
#: - ``"slot"``：每个事件都重算权重（现状，也是「每 15 分钟计算一次」的字面读法）。
#: - ``"daily"`` / ``"monthly"``：只在交易日 / 自然月的第一个事件上重算。
#: - ``"entry"``：**开仓时点定死**，之后不再随信号强弱与杠杆漂移改动。
#:
#: 研报只说「满足开仓条件的品种等权分配资金」，没说多久重算一次权重；而 §1.6 那一档
#: 的措辞是「+ **开仓时点**信号强弱」，字面上更像 ``"entry"``。
#: ⚠️ 粗节拍下等权不再随时成立：新品种进场时旧仓位不缩，总杠杆会漂。这是这条读法的
#: 代价，不是实现缺陷。
REBALANCE_CADENCES = ("slot", "daily", "monthly", "entry")

#: 跑研报的哪一档（D23）。
#:
#: - ``"full"``：最终策略 —— 四道闸 + U2P 强弱 + `Lev_ATR` × `Mul_vol`。
#: - ``"crossover"``：§2.1 的**基线档** —— 只有均线方向与距离走阔，满仓 ±1，
#:   没有空仓状态，不乘任何杠杆。研报自报 13.06% / 夏普 1.03 的那一行。
SIGNAL_TIERS = ("full", "crossover")

_REQUIRED_COLUMNS = (
    "product",
    "contract",
    "trade_date",
    "slot_end",
    "high",
    "low",
    "close",
    "no_trade",
    "adj_factor",
    "continuity_segment",
    "fill_price",
)


class TurnoverCause(StrEnum):
    """换手四分解（计划 §6 风险 ①）。加总必须等于总换手。"""

    SIGNAL = "signal"
    UNIVERSE = "universe"
    REBALANCE = "rebalance"
    ROLL = "roll"


@dataclass(frozen=True, slots=True)
class BacktestParams:
    """预注册网格里的一个点，外加两个矛盾开关（D2 / D3）。"""

    ema_short: int = 12
    ema_long: int = 26
    tnr_window: int = 20
    atr_window: int = 20
    dtnr_k: int = 3
    cost_bps: float = DEFAULT_COST_BPS
    ma_orientation: str = "paper"
    tnr_sign: str = "positive"
    dtnr_mode: str = "mean"
    exit_gates: str = "wide"
    rebalance: str = "slot"
    signal_tier: str = "full"
    min_observations: int = TRADING_DAYS_PER_YEAR

    def __post_init__(self) -> None:
        for name in ("ema_short", "ema_long", "tnr_window", "atr_window", "dtnr_k"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"backtest_params: {name} 必须是 >= 1 的整数；got {value!r}")
        if self.ema_short >= self.ema_long:
            raise ValueError(
                "backtest_params: 短均线跨度必须小于长均线；"
                f"got short={self.ema_short} long={self.ema_long}"
            )
        if not math.isfinite(self.cost_bps) or self.cost_bps < 0:
            raise ValueError(f"backtest_params: cost_bps 必须有限且非负；got {self.cost_bps!r}")
        if self.rebalance not in REBALANCE_CADENCES:
            raise ValueError(
                f"backtest_params: rebalance 只接受 {REBALANCE_CADENCES}；got {self.rebalance!r}"
            )
        if self.signal_tier not in SIGNAL_TIERS:
            raise ValueError(
                f"backtest_params: signal_tier 只接受 {SIGNAL_TIERS}；got {self.signal_tier!r}"
            )
        if self.exit_gates not in EXIT_RULES:
            raise ValueError(
                f"backtest_params: exit_gates 只接受 {EXIT_RULES}；got {self.exit_gates!r}"
            )
        if self.dtnr_mode not in ("mean", "lag"):
            raise ValueError(
                "backtest_params: dtnr_mode 只接受 'mean'（公式图）或 'lag'（正文）；"
                f"got {self.dtnr_mode!r}"
            )

    @property
    def warmup_bars(self) -> int:
        """预热所需的**已成交** bar 数。

        研报没写预热。三条指标各有各的起点：`delta_tnr` 要 `N + k − 1` 根才非 NaN，
        `atr_series` 窗口未满时取部分均值，`ema` 从第一根就有值但那是种子不是均值。
        取三者的上界，并把 EMA 的预热定成它自己的跨度 —— 那时种子的残余权重
        `(1−α)^span ≈ e^{−2} ≈ 13.5%`。这是显式假设，须进保真度报告。
        """
        # ⚠️ 滞后版要多一根：`TNR_t − TNR_{t−k}` 在第 `k+1` 个 TNR 上才有定义，
        # 均值版在第 `k` 个上就有了。
        noise = self.tnr_window + self.dtnr_k - (0 if self.dtnr_mode == "lag" else 1)
        return max(self.ema_long, noise, self.atr_window)


@dataclass(frozen=True)
class BacktestResult:
    """一次回测的全部产物。每一张都要能独立复核。"""

    daily: pd.DataFrame
    shadow_daily: pd.DataFrame
    positions: pd.DataFrame
    executions: pd.DataFrame
    turnover_by_cause: pd.DataFrame
    leverage: pd.DataFrame
    deferred_fills: pd.DataFrame
    params: BacktestParams = field(default_factory=BacktestParams)


# ---------------------------------------------------------------------------
# 第一层：逐品种的信号序列
# ---------------------------------------------------------------------------


def _segment_signals(frame: pd.DataFrame, *, params: BacktestParams) -> pd.DataFrame:
    """一个 `(品种, 连续段)` 的逐 bar 信号。`frame` 必须已按 `slot_end` 排好。"""
    out = frame.copy()
    # ⚠️ 这几列必须先留**空**。预填成 0.0 / "flat" 之后 `ffill` 就没有可填的位置，
    # 空 bar 上的「信号延续」会静默退化成「信号归零」—— 方向恰好相反。
    out["atr"] = np.nan
    out["atr_leverage"] = np.nan
    out["delta_tnr"] = np.nan
    out["u2p"] = np.nan
    out["direction"] = None
    out["signal"] = np.nan
    out["warm"] = None

    traded = out.loc[~out["no_trade"].astype(bool) & out["close"].notna()]
    if traded.empty:
        return out

    factor = traded["adj_factor"].to_numpy(dtype="float64")
    closes = traded["close"].to_numpy(dtype="float64") * factor
    highs = traded["high"].to_numpy(dtype="float64") * factor
    lows = traded["low"].to_numpy(dtype="float64") * factor

    short = ema(closes, span=params.ema_short)
    long = ema(closes, span=params.ema_long)
    widening = gap_widening(short, long)
    tnr = tnr_series(closes, window=params.tnr_window)
    dtnr = delta_tnr(tnr, k=params.dtnr_k, mode=params.dtnr_mode)
    atr = atr_series(highs, lows, closes, window=params.atr_window)

    warmup = params.warmup_bars
    leverages: list[float] = []
    longs: list[bool] = []
    shorts: list[bool] = []
    for index in range(closes.size):
        leverage = atr_leverage(close=closes[index], atr=float(atr[index]))
        leverages.append(leverage)
        warm = index >= warmup and not math.isnan(dtnr[index])
        if not warm:
            longs.append(False)
            shorts.append(False)
            continue
        is_long, is_short = gate_flags(
            short_above_long=bool(short[index] > long[index]),
            widening=bool(widening[index]),
            atr_leverage=leverage,
            delta_tnr=float(dtnr[index]),
            tnr_sign=params.tnr_sign,
            ma_orientation=params.ma_orientation,
        )
        longs.append(is_long)
        shorts.append(is_short)

    # U2P 的递推只走已成交的 bar：空 K 线不是一次「没触发」，它根本没有观测。
    path = _up_down_prob(long_flags=longs, short_flags=shorts)

    # ⚠️ 仓位要整段一起算：`exit_gates="narrow"` 是个状态机，逐 bar 独立判定得不出
    # 「持仓期间只有 Lev_ATR<1 或均线反向才离场」。预热未完成的那几根喂成永不开仓的
    # 输入，而不是事后再抹掉 —— 抹掉会让状态机以为它们曾经持过仓。
    warm_flags = [
        index >= warmup and not math.isnan(dtnr[index]) for index in range(closes.size)
    ]
    if params.signal_tier == "crossover":
        # 基线档不看 Lev_ATR 与 ΔTNR，但**预热照旧**：两档必须从同一根 bar 起跑，
        # 否则它们之间差的就不止研报说的那三样改进了。
        signals_path = crossover_path(
            short_above_long=[bool(short[i] > long[i]) for i in range(closes.size)],
            widening=[bool(widening[i]) and warm_flags[i] for i in range(closes.size)],
            ma_orientation=params.ma_orientation,
        )
    else:
        signals_path = position_path(
            short_above_long=[bool(short[i] > long[i]) for i in range(closes.size)],
            widening=[bool(widening[i]) and warm_flags[i] for i in range(closes.size)],
            atr_leverage=[
                leverages[i] if warm_flags[i] else 0.0 for i in range(closes.size)
            ],
            delta_tnr=[
                float(dtnr[i]) if warm_flags[i] else float("-inf")
                for i in range(closes.size)
            ],
            u2p=list(path),
            tnr_sign=params.tnr_sign,
            ma_orientation=params.ma_orientation,
            exit_gates=params.exit_gates,
        )
    directions = [signal.direction.value for signal in signals_path]
    values = [signal.value for signal in signals_path]

    index = traded.index
    out.loc[index, "atr"] = atr
    out.loc[index, "atr_leverage"] = leverages
    out.loc[index, "delta_tnr"] = dtnr
    out.loc[index, "u2p"] = path
    out.loc[index, "direction"] = directions
    out.loc[index, "signal"] = values
    out.loc[index, "warm"] = [
        i >= warmup and not math.isnan(dtnr[i]) for i in range(closes.size)
    ]

    # 空 bar 上「信号延续」（附录一）：向前填充，段首之前保持空仓。
    carried = ("atr", "atr_leverage", "delta_tnr", "u2p", "direction", "signal", "warm")
    out[list(carried)] = out[list(carried)].ffill()
    out["warm"] = out["warm"].fillna(False).astype(bool)
    out["direction"] = out["direction"].fillna(Direction.FLAT.value)
    for column in ("atr_leverage", "u2p", "signal"):
        out[column] = out[column].fillna(0.0).astype("float64")
    return out


def _up_down_prob(*, long_flags: Sequence[bool], short_flags: Sequence[bool]) -> list[float]:
    """`signals.up_down_prob` 的 U2P 分量。分开一层是为了让空序列也有确定的返回。"""
    if not long_flags:
        return []
    from cta_continuous.signals import up_down_prob

    return list(up_down_prob(long_flags=long_flags, short_flags=short_flags).u2p)


def product_signals(panel: pd.DataFrame, *, params: BacktestParams) -> pd.DataFrame:
    """面板 → 逐 bar 的方向、信号值、ATR 杠杆、是否预热完成。

    按 `(product, continuity_segment)` 隔离：新段的 EMA / ATR / TNR / U2P 全部重新
    预热，旧段的状态一点都不许带过来。
    """
    _require_columns(panel)
    if panel.empty:
        return panel.assign(
            atr=pd.Series(dtype="float64"),
            atr_leverage=pd.Series(dtype="float64"),
            delta_tnr=pd.Series(dtype="float64"),
            u2p=pd.Series(dtype="float64"),
            direction=pd.Series(dtype="object"),
            signal=pd.Series(dtype="float64"),
            warm=pd.Series(dtype="bool"),
        )

    ordered = panel.sort_values(["product", "continuity_segment", "slot_end"])
    pieces = [
        _segment_signals(group, params=params)
        for _key, group in ordered.groupby(
            ["product", "continuity_segment"], sort=True, observed=True
        )
    ]
    return pd.concat(pieces).sort_values(["slot_end", "product"]).reset_index(drop=True)


def _require_columns(panel: pd.DataFrame) -> None:
    missing = [column for column in _REQUIRED_COLUMNS if column not in panel.columns]
    if missing:
        raise ValueError(
            f"backtest_panel_columns: 面板缺列 {missing}；"
            "面板必须由 `cta_continuous.panel.build_panel` 产出"
        )


# ---------------------------------------------------------------------------
# 第二层：组合
# ---------------------------------------------------------------------------


def _fill_schedule(signals: pd.DataFrame) -> pd.DataFrame:
    """给每根 bar 配上它的**成交时刻**与**成交所属交易日**。

    一天最后一根的成交窗在下一时段的前 5 分钟（D14），所以它的成交时刻由**下一根
    bar** 反推：`next_slot_end − (15 − 5) 分钟`。所属交易日也随之落到下一天 ——
    否则隔夜跳空会被记进前一天，而日收益正是波动率反馈的输入。
    """
    out = signals.sort_values(["product", "slot_end"]).copy()
    grouped = out.groupby("product", sort=False, observed=True)
    next_slot = grouped["slot_end"].shift(-1)
    next_date = grouped["trade_date"].shift(-1)
    same_day = out["trade_date"].eq(next_date)

    regular = out["slot_end"] + pd.Timedelta(minutes=FILL_MINUTES)
    deferred = next_slot - pd.Timedelta(minutes=BUCKET_MINUTES - FILL_MINUTES)
    out["fill_time"] = regular.where(same_day, deferred)
    out["fill_trade_date"] = out["trade_date"].where(same_day, next_date)
    return out.sort_values(["fill_time", "product"]).reset_index(drop=True)


def _monthly_universe(signals: pd.DataFrame) -> dict[tuple[int, int], frozenset[str]]:
    """D10：宇宙按月定。月内某天没有行情是「不交易」，不是「掉出宇宙」。"""
    months = signals["trade_date"].dt.to_period("M")
    universe: dict[tuple[int, int], frozenset[str]] = {}
    for period, group in signals.groupby(months, sort=True, observed=True):
        universe[(period.year, period.month)] = frozenset(group["product"].unique())
    return universe


@dataclass
class _ProductState:
    contract: str
    direction: str = Direction.FLAT.value
    signal: float = 0.0
    atr_leverage: float = 0.0
    atr: float = float("nan")
    close_adj: float = float("nan")
    warm: bool = False
    fill_price: float = float("nan")
    segment: int = 0


def run_backtest(
    panel: pd.DataFrame,
    *,
    params: BacktestParams = BacktestParams(),
    signals: pd.DataFrame | None = None,
) -> BacktestResult:
    """全历史组合回测。

    `signals` 可以外部传入，让 12 次网格共用同一份指标计算 —— 也让组合层的用例
    不必去凑一条同时开四道闸的价格路径。

    ## 谁在这一刻可以成交

    只有**本事件确实有成交价**的品种才允许改仓。没有成交价（成交窗零成交，或本刻
    根本没有该品种的 bar）就维持上一档权重 —— 拿上一次的成交价当本次的成交价，
    等于凭空造一笔成交（D19，`panel.py` 已经拒绝过就地换价）。

    唯一的例外是**掉出宇宙**：该品种整月不再有行情，等它「下一个有价的槽」就是等
    不到了。这一笔按最后一次观测到的成交价平掉，并标成 `TurnoverCause.UNIVERSE`。
    """
    _require_columns(panel)
    frame = product_signals(panel, params=params) if signals is None else signals
    if frame.empty:
        return _empty_result(params)

    schedule = _fill_schedule(frame)
    universe = _monthly_universe(frame)

    account = EventAccount(cost_bps=params.cost_bps)
    shadow = EventAccount(cost_bps=params.cost_bps)
    account.initialize({})
    shadow.initialize({})

    state: dict[str, _ProductState] = {}
    #: 品种 → （承载它权重的合约, 权重）。展期时合约变、权重不变。
    book: dict[str, tuple[str, float]] = {}
    shadow_book: dict[str, tuple[str, float]] = {}
    last_price: dict[str, float] = {}

    deferred: list[dict[str, object]] = []
    position_rows: list[dict[str, object]] = []
    execution_rows: list[dict[str, object]] = []
    leverage_rows: list[dict[str, object]] = []
    daily_rows: list[dict[str, object]] = []
    shadow_daily_rows: list[dict[str, object]] = []
    shadow_returns: list[tuple[date, float]] = []

    open_day: date | None = None
    previous_day: date | None = None
    day_last_fill = None

    for (fill_time, fill_day), events in schedule.groupby(
        ["fill_time", "fill_trade_date"], sort=True, observed=True
    ):
        if pd.isna(fill_time):
            continue
        day = fill_day.date() if hasattr(fill_day, "date") else fill_day
        if open_day is not None and day != open_day:
            _close_day(
                account=account,
                shadow=shadow,
                day=open_day,
                timestamp=day_last_fill,
                prices=last_price,
                book=book,
                shadow_book=shadow_book,
                daily_rows=daily_rows,
                shadow_daily_rows=shadow_daily_rows,
                shadow_returns=shadow_returns,
            )
        resize_now = _resize_allowed(
            params.rebalance, day=day, opened=open_day, previous=previous_day
        )
        previous_day = day
        open_day = day
        # ⚠️ 日切时刻取**当日最后一个事件时刻**，而不是「最后一次换仓的时刻」：
        # 一整天没有换仓时后者还停在前一天，`mark_close` 会因为日期对不上硬失败。
        day_last_fill = fill_time.to_pydatetime()

        # 1) 用这一刻可成交的 bar 更新品种状态。
        priced_now: set[str] = set()
        for row in events.itertuples(index=False):
            product = str(row.product)
            contract = str(row.contract)
            price = float(row.fill_price) if pd.notna(row.fill_price) else float("nan")
            state[product] = _ProductState(
                contract=contract,
                direction=str(row.direction),
                signal=float(row.signal),
                atr_leverage=float(row.atr_leverage),
                atr=float(row.atr) if pd.notna(row.atr) else float("nan"),
                close_adj=(
                    float(row.close) * float(row.adj_factor)
                    if pd.notna(row.close)
                    else float("nan")
                ),
                warm=bool(row.warm),
                fill_price=price,
                segment=int(row.continuity_segment),
            )
            if math.isfinite(price) and price > 0.0:
                last_price[contract] = price
                priced_now.add(product)
            else:
                deferred.append(
                    {
                        "product": product,
                        "contract": contract,
                        "trade_date": row.trade_date,
                        "slot_end": row.slot_end,
                        "reason": "fill_unpriceable",
                    }
                )

        # 2) 波动率乘数：月内不变，只用上月末为止的影子收益（D17）。
        realized = monthly_realized_volatility(
            session_date=day,
            returns_by_date=shadow_returns,
            min_observations=params.min_observations,
        )
        if realized is not None and realized <= 0.0:
            realized = None

        # 3) 等权：只在**此刻满足开仓条件**的品种之间分资金。
        members = universe.get((day.year, day.month), frozenset())
        active = sorted(
            product
            for product, value in state.items()
            if product in members
            and value.warm
            and value.direction != Direction.FLAT.value
        )
        share = 1.0 / len(active) if active else 0.0

        desired: dict[str, float] = {}
        shadow_desired: dict[str, float] = {}
        for product in active:
            value = state[product]
            if params.signal_tier == "crossover":
                # 基线档「均满仓开仓」：等权份额就是仓位，不乘 Lev_ATR、也不乘
                # Mul_vol —— 连它那 252 日自举期一并不适用，否则会白丢起手一年。
                leverage = 1.0
            else:
                leverage = (
                    0.0
                    if realized is None or not math.isfinite(value.close_adj)
                    else final_leverage(
                        close=value.close_adj, atr=value.atr, realized_vol=realized
                    )
                )
            desired[product] = share * leverage * value.signal
            shadow_desired[product] = share * value.atr_leverage * value.signal
            position_rows.append(
                {
                    "slot_end": fill_time,
                    "trade_date": day,
                    "product": product,
                    "contract": value.contract,
                    "capital_share": share,
                    "leverage": leverage,
                    "atr_leverage": value.atr_leverage,
                    "signal": value.signal,
                    "weight": desired[product],
                    "continuity_segment": value.segment,
                }
            )

        # 4) 谁真的能改仓。
        next_book = dict(book)
        next_shadow = dict(shadow_book)
        causes: dict[str, TurnoverCause] = {}
        for product in sorted(set(state) | set(book)):
            held = book.get(product)
            in_universe = product in members
            if not in_universe:
                if held is not None and held[1] != 0.0:
                    next_book[product] = (held[0], 0.0)
                    next_shadow[product] = (held[0], 0.0)
                    causes[product] = TurnoverCause.UNIVERSE
                continue
            if product not in priced_now:
                continue
            value = state[product]
            want = desired.get(product, 0.0)
            shadow_want = shadow_desired.get(product, 0.0)
            # ⚠️ 成因按**权重的有无**判，不按方向标签。`Mul_vol` 还没攒够时品种已经
            # 「有方向但权重为 0」，按标签判会把那一笔记成建仓；等杠杆真正可用时方向
            # 没变、于是被归成再平衡 —— `rebalance="entry"` 下就永远建不了仓了。
            carried = held[1] if held is not None else 0.0
            shadow_carried = shadow_book.get(product, (None, 0.0))[1]
            # ⚠️ 「跨越零」要看**两本账**。波动率自举期间主账户权重恒为 0（`Mul_vol`
            # 还没有），只有影子在建仓；只看主账户会把影子的首次建仓判成再平衡，
            # 于是被粗节拍挡掉 ⇒ 影子收益恒 0 ⇒ 波动率永远算不出来 ⇒ 整段回测一笔
            # 不成交。这条只有接线才看得见。
            # ⚠️ 「换方向」有两种走法，两种都必须算 SIGNAL：经过空仓的（跨越零），
            # 和**直接反手**的（权重从 +w 跳到 −w，一次也没碰过 0）。只判跨越零会把
            # 反手归成再平衡 ⇒ entry / monthly / daily 下被节拍闸挡掉 ⇒ 品种建仓后
            # 再不换方向。研报基线档没有空仓状态、方向变化**全部**是直接反手，
            # 全档在 `exit_gates="narrow"` 下也能直接反手。
            crosses = (
                ((carried == 0.0) != (want == 0.0))
                or ((shadow_carried == 0.0) != (shadow_want == 0.0))
                or (carried * want < 0.0)
                or (shadow_carried * shadow_want < 0.0)
            )
            if (
                held is not None
                and held[0] != value.contract
                and carried != 0.0
                and want != 0.0
            ):
                cause = TurnoverCause.ROLL
            elif crosses:
                cause = TurnoverCause.SIGNAL
            else:
                cause = TurnoverCause.REBALANCE
            if held is not None and held[0] == value.contract and held[1] == want:
                continue
            # ⚠️ 节拍只挡**再平衡**：开仓、平仓、展期在任何节拍下都照常成交，
            # 否则「持仓期间不改仓位」会退化成「永远不开仓」。
            if cause is TurnoverCause.REBALANCE and not resize_now:
                continue
            next_book[product] = (value.contract, want)
            next_shadow[product] = (value.contract, shadow_want)
            causes[product] = cause

        if not causes:
            continue

        targets, _ = _contract_targets(book, next_book, causes)
        shadow_targets, _ = _contract_targets(shadow_book, next_shadow, causes)
        # ⚠️ 成因表要覆盖**两本账**出现过的合约：本体权重为 0 的品种已从 `book` 里
        # 摘掉，影子却可能还持有着它，那一笔换仓不给成因就会硬失败。
        reasons = _reasons(
            causes, (book, shadow_book, next_book, next_shadow)
        )
        prices = _event_prices(
            books=(book, shadow_book),
            targets=(targets, shadow_targets),
            last_price=last_price,
        )
        if prices is None:
            continue

        timestamp = fill_time.to_pydatetime()
        event = account.rebalance(timestamp, prices, targets, reasons)
        shadow.rebalance(timestamp, prices, shadow_targets, reasons)
        book = {p: v for p, v in next_book.items() if v[1] != 0.0}
        shadow_book = {p: v for p, v in next_shadow.items() if v[1] != 0.0}

        for record in event.executions:
            execution_rows.append(
                {
                    "timestamp": record.timestamp,
                    "trade_date": day,
                    "contract": record.contract,
                    "price": record.price,
                    "old_weight": record.old_weight,
                    "new_weight": record.new_weight,
                    "turnover": record.turnover,
                    "cost": record.cost,
                    "cause": record.reason,
                }
            )
        leverage_rows.append(
            {
                "timestamp": timestamp,
                "trade_date": day,
                "realized_vol": realized,
                "active": len(active),
                "gross_leverage": event.gross_leverage,
            }
        )

    if open_day is not None:
        _close_day(
            account=account,
            shadow=shadow,
            day=open_day,
            timestamp=day_last_fill,
            prices=last_price,
            book=book,
            shadow_book=shadow_book,
            daily_rows=daily_rows,
            shadow_daily_rows=shadow_daily_rows,
            shadow_returns=shadow_returns,
        )

    executions = pd.DataFrame(
        execution_rows,
        columns=[
            "timestamp", "trade_date", "contract", "price", "old_weight",
            "new_weight", "turnover", "cost", "cause",
        ],
    )
    by_cause = (
        executions.groupby("cause", as_index=False, observed=True)[["turnover", "cost"]].sum()
        if not executions.empty
        else pd.DataFrame(columns=["cause", "turnover", "cost"])
    )
    return BacktestResult(
        daily=pd.DataFrame(daily_rows),
        shadow_daily=pd.DataFrame(shadow_daily_rows),
        positions=pd.DataFrame(
            position_rows,
            columns=[
                "slot_end", "trade_date", "product", "contract", "capital_share",
                "leverage", "atr_leverage", "signal", "weight", "continuity_segment",
            ],
        ),
        executions=executions,
        turnover_by_cause=by_cause,
        leverage=pd.DataFrame(leverage_rows),
        deferred_fills=pd.DataFrame(
            deferred, columns=["product", "contract", "trade_date", "slot_end", "reason"]
        ),
        params=params,
    )


def _resize_allowed(
    cadence: str, *, day: date, opened: date | None, previous: date | None
) -> bool:
    """这一刻允不允许**再平衡**（D22）。粗节拍只在周期的第一个事件上放行。"""
    if cadence == "slot":
        return True
    if cadence == "entry":
        return False
    if opened is not None and day == opened:
        return False                       # 同一个交易日的后续事件
    if cadence == "daily":
        return True
    return previous is None or (day.year, day.month) != (previous.year, previous.month)


def _contract_targets(
    book: Mapping[str, tuple[str, float]],
    next_book: Mapping[str, tuple[str, float]],
    causes: Mapping[str, TurnoverCause],
) -> tuple[dict[str, float], dict[str, str]]:
    """品种账 → 合约权重与逐合约的换手成因。

    展期时旧合约必须**显式**归零：账户按合约记权重，不写它就会被当成还持有着。
    """
    targets: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for product, (contract, weight) in next_book.items():
        targets[contract] = targets.get(contract, 0.0) + weight
    for product, (contract, weight) in book.items():
        if weight == 0.0:
            continue
        if next_book.get(product, (contract, 0.0))[0] != contract:
            targets.setdefault(contract, 0.0)
    return targets, reasons


def _reasons(
    causes: Mapping[str, TurnoverCause],
    books: Sequence[Mapping[str, tuple[str, float]]],
) -> dict[str, str]:
    """逐合约的换手成因。一个品种在展期那一刻会同时占着两张合约。"""
    reasons: dict[str, str] = {}
    for product, cause in causes.items():
        for book in books:
            entry = book.get(product)
            if entry is not None:
                reasons[entry[0]] = cause.value
    return reasons


def _event_prices(
    *,
    books: Sequence[Mapping[str, tuple[str, float]]],
    targets: Sequence[Mapping[str, float]],
    last_price: Mapping[str, float],
) -> dict[str, float] | None:
    """账户要「持有的 ∪ 变动的」那些合约的价。缺一个就整笔顺延。

    ⚠️ 本体与影子是两本独立的账，持仓集合会分岔：波动率历史不够时本体权重是 0，
    影子却照常持有。所以取价要覆盖**两本账的并集**，否则影子那一笔会缺价硬失败。
    """
    required_set: set[str] = set()
    for book, target in zip(books, targets):
        current = _flatten(book)
        required_set |= {c for c, w in current.items() if w != 0.0}
        required_set |= {c for c, w in target.items() if w != current.get(c, 0.0)}
    required = sorted(required_set)
    prices: dict[str, float] = {}
    for contract in required:
        price = last_price.get(contract)
        if price is None or not math.isfinite(price) or price <= 0.0:
            return None
        prices[contract] = price
    return prices


def _flatten(book: Mapping[str, tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for contract, weight in book.values():
        out[contract] = out.get(contract, 0.0) + weight
    return out


def _close_day(
    *,
    account: EventAccount,
    shadow: EventAccount,
    day: date,
    timestamp,
    prices: Mapping[str, float],
    book: Mapping[str, tuple[str, float]],
    shadow_book: Mapping[str, tuple[str, float]],
    daily_rows: list[dict[str, object]],
    shadow_daily_rows: list[dict[str, object]],
    shadow_returns: list[tuple[date, float]],
):
    """收一天的账，返回收盘事件的时刻。

    ⚠️ 不建仓的日子影子收益是 0，但**必须记进序列** —— 少记会让波动率窗口整体错位，
    而且错得看不出来：波动率仍会是个合理数字。

    ⚠️ 收盘时刻取「当日最后一个事件 + 1 秒」，不能取当日 23:59：下一交易日的夜盘
    起在**本自然日** 21:00，取 23:59 会让事件时刻不再单调。
    """
    if timestamp is None:
        shadow_returns.append((day, 0.0))
        return None
    close_at = timestamp + timedelta(seconds=1)
    if close_at.date() != day:
        raise ValueError(
            "backtest_day_boundary: 收盘时刻落在了交易日之外；"
            f"day={day} last_event={timestamp.isoformat()}"
        )
    needed = {c for c, w in _flatten(book).items() if w != 0.0}
    needed |= {c for c, w in _flatten(shadow_book).items() if w != 0.0}
    missing = sorted(needed - set(prices))
    if missing:
        raise ValueError(
            f"backtest_close_price_missing: 收盘缺价；day={day} first={missing[0]!r}"
        )
    marks = {contract: prices[contract] for contract in needed}
    account.mark_close(day, close_at, marks)
    shadow.mark_close(day, close_at, marks)
    row = account.drain_daily_row(day, boundary_type="session")
    shadow_row = shadow.drain_daily_row(day, boundary_type="session")
    daily_rows.append(_daily_dict(row))
    shadow_daily_rows.append(_daily_dict(shadow_row))
    shadow_returns.append((day, shadow_row.net_return))
    return close_at


def _daily_dict(row) -> dict[str, object]:
    return {
        "trade_date": row.trade_date,
        "gross_return": row.gross_return,
        "net_return": row.net_return,
        "turnover": row.turnover,
        # ⚠️ 两个成本不是一回事：`cost` 是复利化的拖累（gross_return − net_return），
        # `direct_cost` 是逐笔成本的直接加总。拿逐笔台账去对账要对后者。
        "cost": row.cost,
        "direct_cost": row.direct_cost,
        "equity": row.equity,
        "gross_equity": row.gross_equity,
        "gross_leverage": row.gross_leverage,
    }


def _empty_result(params: BacktestParams) -> BacktestResult:
    empty = pd.DataFrame()
    return BacktestResult(
        daily=empty,
        shadow_daily=empty,
        positions=empty,
        executions=empty,
        turnover_by_cause=pd.DataFrame(columns=["cause", "turnover", "cost"]),
        leverage=empty,
        deferred_fills=pd.DataFrame(
            columns=["product", "contract", "trade_date", "slot_end", "reason"]
        ),
        params=params,
    )
