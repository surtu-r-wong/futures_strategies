"""组合回测（计划 Task 6）。

研报 §5.1：满足开仓条件的品种**等权分配资金**，权重再乘 `Lev × Signal`；成交价是
信号那根 bar 之后 5 分钟的 VWAP，成本 1.3 个基点。附录一：非交易时段行情置空、
信号延续、**收益率置零**。

## 两层分开测

指标层（`product_signals`）用一条**手工验证过**的价格路径：锯齿的上行腿逐步变长、
回撤腿逐步变短，于是 TNR 严格上升、ΔTNR 恒正，四道闸在 i=8/10/12 三根上打开。
期望的概率值直接取研报图 13 的字面量（75% / 87.5% / 93.75%）。

组合层（`run_backtest`）直接喂构造好的 signals 帧。研报的四道闸里 `ΔTNR > 0` 要求
TNR 严格上升，而 TNR 上界是 1 —— 光滑趋势里它会钉在 1、噪音闸永久关闭。也就是说
「一条让信号持续开着的价格路径」本身就不是这个策略的常态，拿它当组合层的夹具只会
把组合逻辑和指标口径搅在一起。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from cta_continuous.backtest import (
    BacktestParams,
    TurnoverCause,
    product_signals,
    run_backtest,
)
from cta_continuous.panel import PANEL_COLUMNS, normalise_panel

SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 指标层用的小参数：预热 = max(8, 2+2−1, 2) = 8 根。
SIGNAL_PARAMS = BacktestParams(
    ema_short=4, ema_long=8, tnr_window=2, atr_window=2, dtnr_k=2, min_observations=2
)

#: 闸打开的三根，以及研报图 13 印出的上涨概率。
GATE_BARS = (8, 10, 12)
FIGURE_13 = (0.75, 0.875, 0.9375)


def _zigzag(count, *, base=10000.0):
    """上行腿恒增、回撤腿恒减的锯齿。

    TNR（窗口 2）= `|d_t + d_{t−1}| / (|d_t| + |d_{t−1}|)`，两条腿越失衡它越大，
    所以这条路径的 TNR **严格上升** ⇒ `ΔTNR > 0` 一直成立；净漂移同时加速，
    两条 EMA 的距离持续扩大。
    """
    closes, level, up, down = [], base, 10.0, 9.0
    for index in range(count):
        if index % 2 == 0:
            level += up
            up += 1.0
        else:
            level -= down
            down -= 1.0
        closes.append(level)
    return closes


def _slot(day, index):
    return datetime(day.year, day.month, day.day, 9, 15, tzinfo=SHANGHAI) + (
        timedelta(minutes=15) * index
    )


def _panel_row(**overrides):
    row = {
        "product": "RB",
        "contract": "RB2405.SHF",
        "trade_date": date(2024, 1, 2),
        "slot_end": None,
        "open": 100.0,
        "high": 100.5,
        "low": 99.5,
        "close": 100.0,
        "volume": 100.0,
        "no_trade": False,
        "adj_factor": 1.0,
        "continuity_segment": 0,
        "fill_price": 100.0,
        "fill_pending": False,
        "fill_unpriceable": False,
        "pricing_basis": "amount_vwap",
        "multiplier": 10,
    }
    row.update(overrides)
    return row


def _price_panel(closes, *, product="RB", contract="RB2405.SHF", day=date(2024, 1, 2),
                 segment=0, silent=()):
    rows = []
    for index, close in enumerate(closes):
        quiet = index in silent
        rows.append(
            _panel_row(
                product=product,
                contract=contract,
                trade_date=day,
                slot_end=_slot(day, index),
                open=close,
                high=close + 0.5,
                low=close - 0.5,
                close=None if quiet else close,
                volume=0.0 if quiet else 100.0,
                no_trade=quiet,
                continuity_segment=segment,
                fill_price=close,
            )
        )
    return normalise_panel(pd.DataFrame(rows, columns=list(PANEL_COLUMNS)))


# --- 指标层 -----------------------------------------------------------------


def test_signals_wait_for_the_segments_own_warmup():
    signals = product_signals(_price_panel(_zigzag(14)), params=SIGNAL_PARAMS)

    assert len(signals) == 14
    assert list(signals["warm"][:8]) == [False] * 8
    assert list(signals["warm"][8:]) == [True] * 6


def test_the_four_gates_open_only_where_the_paper_says_they_do():
    """闸打开的那几根是路径决定的，不是被测代码决定的 —— 期望值由 §1.2 的公式手算。"""
    signals = product_signals(_price_panel(_zigzag(14)), params=SIGNAL_PARAMS)

    opened = [i for i, d in enumerate(signals["direction"]) if d == "long"]
    assert opened == list(GATE_BARS)
    assert set(signals["direction"]) == {"flat", "long"}


def test_the_probability_path_reproduces_figure_13():
    """研报图 13：每次同向触发把对侧概率折半搬过来 —— 75% / 87.5% / 93.75%。"""
    signals = product_signals(_price_panel(_zigzag(14)), params=SIGNAL_PARAMS)

    values = [float(signals["signal"].iloc[bar]) for bar in GATE_BARS]
    assert values == pytest.approx(list(FIGURE_13))
    # 闸不过的那几根是**空仓**（D6），不是持有到反向。
    assert float(signals["signal"].iloc[9]) == 0.0
    assert float(signals["signal"].iloc[11]) == 0.0
    # 但概率本身不回退：U2P 在没触发的那根上保持不变（图 13 的横向段）。
    assert float(signals["u2p"].iloc[9]) == pytest.approx(float(signals["u2p"].iloc[8]))


def test_a_new_continuity_segment_cannot_signal_before_renewed_warmup():
    """EMA / ATR / TNR / U2P 不得跨断代携带状态。"""
    closes = _zigzag(14)
    first = _price_panel(closes, segment=0, day=date(2024, 1, 2))
    second = _price_panel(closes, segment=1, day=date(2024, 1, 3))
    signals = product_signals(
        pd.concat([first, second], ignore_index=True), params=SIGNAL_PARAMS
    )

    fresh = signals.loc[signals["continuity_segment"] == 1].reset_index(drop=True)
    assert list(fresh["warm"][:8]) == [False] * 8
    assert set(fresh["direction"][:8]) == {"flat"}
    # 新段从 0.5/0.5 重新起步：第一次触发后 U2P 又是 0.5，不是旧段末尾的值。
    assert float(fresh["u2p"].iloc[8]) == pytest.approx(0.5)
    assert float(signals.loc[signals["continuity_segment"] == 0, "u2p"].iloc[-1]) > 0.5


def test_no_trade_bars_do_not_advance_the_recursion():
    """D13：空 K 线带的是结转价，不入 ATR/TNR 窗口，也不推进 U2P —— 只延续。"""
    closes = _zigzag(14)
    dense = product_signals(_price_panel(closes), params=SIGNAL_PARAMS)
    sparse = product_signals(_price_panel(closes, silent=(13,)), params=SIGNAL_PARAMS)

    assert bool(sparse["no_trade"].iloc[13]) is True
    assert float(sparse["u2p"].iloc[13]) == pytest.approx(float(sparse["u2p"].iloc[12]))
    assert sparse["direction"].iloc[13] == sparse["direction"].iloc[12]
    # 前缀不受影响：空 bar 只是没参与，不是改了口径。
    assert list(sparse["u2p"][:13]) == pytest.approx(list(dense["u2p"][:13]))


def test_product_signals_refuses_a_panel_it_does_not_recognise():
    frame = _price_panel(_zigzag(14)).drop(columns=["fill_price"])

    with pytest.raises(ValueError) as caught:
        product_signals(frame, params=SIGNAL_PARAMS)

    assert str(caught.value).startswith("backtest_panel_columns:")


# --- 组合层 -----------------------------------------------------------------

FAST = BacktestParams(
    ema_short=2, ema_long=3, tnr_window=2, atr_window=2, dtnr_k=2, min_observations=2
)


def _signals(rows):
    """构造好的 signals 帧：面板列 + 指标层产出的列。"""
    frame = normalise_panel(
        pd.DataFrame([{k: v for k, v in row.items() if k in PANEL_COLUMNS} for row in rows],
                     columns=list(PANEL_COLUMNS))
    )
    for column, default in (
        ("atr", 1.0), ("atr_leverage", 4.0), ("delta_tnr", 0.1),
        ("u2p", 0.5), ("direction", "long"), ("signal", 0.75), ("warm", True),
    ):
        frame[column] = [row.get(column, default) for row in rows]
    return frame


#: 组合层夹具的价格摆动。⚠️ 不能用常数价：那样影子收益在整个波动率窗口里恒为 0、
#: 标准差是 0，`Mul_vol` 于是永远算不出来，杠杆恒为 0，一笔成交都不会发生 ——
#: 看上去像「回测没跑」，其实是夹具退化。
_WIGGLE = (1.0, 1.004, 0.998, 1.002)

#: 信号强度也要动。⚠️ 恒定信号 + 被截到 4 倍的杠杆 ⇒ 权重全程不变，整段回测只有
#: 建仓那一两笔成交，于是「这一笔没成交」根本无从对照 —— 夹具退化的第二种样子。
#: U2P 本来就随每次同向触发单调逼近 1，逐 bar 变化才是它的常态。
_SIGNAL_CYCLE = (0.70, 0.78, 0.86, 0.94)


def _series(*, product, contract, days, bars=3, close=1000.0, direction="long",
            signal=0.75, warm=True, segment=0, atr_leverage=4.0, fill=None):
    rows = []
    step = 0
    for day in days:
        for index in range(bars):
            level = close * _WIGGLE[step % len(_WIGGLE)]
            strength = _SIGNAL_CYCLE[step % len(_SIGNAL_CYCLE)]
            step += 1
            rows.append(
                _panel_row(
                    product=product,
                    contract=contract,
                    trade_date=day,
                    slot_end=_slot(day, index),
                    close=level,
                    high=level + 0.5,
                    low=level - 0.5,
                    fill_price=level if fill is None else fill,
                    continuity_segment=segment,
                )
                | {
                    "atr": 1.0,
                    "atr_leverage": atr_leverage,
                    "delta_tnr": 0.1,
                    "u2p": 0.5,
                    "direction": direction,
                    "signal": (
                        0.0
                        if direction == "flat"
                        else (strength if signal > 0 else -strength)
                    ),
                    "warm": warm,
                }
            )
    return rows


def _days(count, *, start=date(2024, 1, 2)):
    return [stamp.date() for stamp in pd.bdate_range(start, periods=count)]


#: ⚠️ 组合层的夹具必须**跨月**。`monthly_realized_volatility` 的窗口在当月 1 号之前
#: 截断（月内不变 + 不用当月自己的收益），所以全落在同一个月里的夹具永远攒不出
#: `Mul_vol`，杠杆恒为 0、一笔成交都不会发生。
SEED = _days(5, start=date(2023, 12, 25))   # 只为影子攒够历史
MONTH1 = _days(5, start=date(2024, 1, 2))
MONTH2 = _days(5, start=date(2024, 2, 1))
ALL_DAYS = SEED + MONTH1 + MONTH2


def test_weights_are_equal_among_products_passing_the_gates_that_slot():
    """研报：满足开仓条件的品种**等权分配资金**。"""
    rows = _series(product="RB", contract="RB2405.SHF", days=ALL_DAYS)
    rows += _series(product="HC", contract="HC2405.SHF", days=ALL_DAYS, close=2000.0)
    rows += _series(
        product="CU", contract="CU2405.SHF", days=ALL_DAYS, close=70000.0,
        direction="flat", signal=0.0,
    )
    frame = _signals(rows)

    result = run_backtest(frame, params=FAST, signals=frame)
    active = result.positions

    assert set(active["product"]) == {"RB", "HC"}      # 闸不过的 CU 不分资金
    assert set(active["capital_share"].round(12)) == {0.5}
    traded = active.loc[active["trade_date"] >= date(2024, 1, 2)]
    assert (traded["weight"].abs() > 0).all()
    assert traded.groupby("product")["weight"].nunique().max() >= 1


def test_a_product_outside_its_trading_session_earns_zero_not_nan():
    """附录一：非交易时段行情置空、信号延续、**收益率置零**。"""
    rows = _series(product="RB", contract="RB2405.SHF", days=ALL_DAYS)
    rows += _series(product="HC", contract="HC2405.SHF", days=SEED + MONTH1[:2], close=2000.0)
    frame = _signals(rows)

    result = run_backtest(frame, params=FAST, signals=frame)

    assert result.daily["gross_return"].notna().all()
    assert result.daily["net_return"].notna().all()
    assert list(result.daily["trade_date"]) == ALL_DAYS


def test_universe_exit_forces_a_close_at_the_last_observed_fill():
    """品种掉出宇宙就得平掉。等「下一个有价的槽」是等不到的 —— 它整月没有行情。"""
    rows = _series(product="RB", contract="RB2405.SHF", days=ALL_DAYS)
    rows += _series(product="HC", contract="HC2405.SHF", days=SEED + MONTH1, close=2000.0)
    frame = _signals(rows)

    result = run_backtest(frame, params=FAST, signals=frame)
    exits = result.executions.loc[result.executions["cause"] == TurnoverCause.UNIVERSE]

    assert set(exits["contract"]) == {"HC2405.SHF"}
    assert (exits["new_weight"] == 0.0).all()
    assert (exits["old_weight"] != 0.0).all()
    # 平在**最后一次观测到的成交价**上 —— 它整月没有行情，等不到下一个有价的槽。
    # ⚠️ 是倒数第二根，不是最后一根：最后一根的成交窗落在「下一时段的前 5 分钟」，
    # 而 HC 再没有下一时段了，那一笔成交价发不出来（D14）。
    last_seen = frame.loc[frame["product"] == "HC"].sort_values("slot_end")
    expected = float(last_seen["fill_price"].iloc[-2])
    assert exits["price"].tolist() == pytest.approx([expected] * len(exits))
    assert set(exits["trade_date"]) == {MONTH2[0]}


def test_turnover_is_attributed_to_the_four_causes():
    """信号 / 宇宙 / 再平衡 / 展期，加总等于总换手。"""
    rows = _series(product="RB", contract="RB2405.SHF", days=SEED + MONTH1)
    rows += _series(product="RB", contract="RB2409.SHF", days=MONTH2, close=1100.0)
    rows += _series(product="HC", contract="HC2405.SHF", days=SEED + MONTH1, close=2000.0)
    rows += _series(
        product="CU", contract="CU2405.SHF", days=ALL_DAYS, close=70000.0,
        direction="short", signal=-0.8,
    )
    frame = _signals(rows)

    result = run_backtest(frame, params=FAST, signals=frame)

    assert set(result.turnover_by_cause["cause"]) <= {c.value for c in TurnoverCause}
    assert result.turnover_by_cause["turnover"].sum() == pytest.approx(
        result.daily["turnover"].sum()
    )
    # 对逐笔台账要用 `direct_cost`：`cost` 是复利化后的拖累，两者不等。
    assert result.turnover_by_cause["cost"].sum() == pytest.approx(
        result.daily["direct_cost"].sum()
    )


def test_a_roll_is_its_own_turnover_cause():
    """换月要平旧开新，两笔都是真实成交 —— 后复权连续价让权重看着没动，钱是真花了。"""
    rows = _series(product="RB", contract="RB2405.SHF", days=SEED + MONTH1)
    rows += _series(product="RB", contract="RB2409.SHF", days=MONTH2, close=1100.0)
    frame = _signals(rows)

    result = run_backtest(frame, params=FAST, signals=frame)
    rolls = result.executions.loc[result.executions["cause"] == TurnoverCause.ROLL]

    assert set(rolls["contract"]) == {"RB2405.SHF", "RB2409.SHF"}
    closed = rolls.loc[rolls["contract"] == "RB2405.SHF"]
    opened = rolls.loc[rolls["contract"] == "RB2409.SHF"]
    assert (closed["new_weight"] == 0.0).all()
    assert (opened["old_weight"] == 0.0).all()
    # 旧合约整仓平掉、新合约整仓建起：两笔各自的换手就是各自的权重绝对值。
    # ⚠️ 两者一般**不相等** —— 换月那一根信号强度通常也变了，那部分缩放一并算进
    # ROLL（一根 bar 上一张合约只能有一个成因）。
    assert closed["turnover"].tolist() == pytest.approx(
        closed["old_weight"].abs().tolist()
    )
    assert opened["turnover"].tolist() == pytest.approx(
        opened["new_weight"].abs().tolist()
    )


def test_no_position_before_the_first_month_with_a_full_year_of_returns():
    """D8：`Mul_vol` 攒不够一年策略收益就不建仓，而不是把乘数默认成 1。"""
    rows = _series(product="RB", contract="RB2405.SHF", days=ALL_DAYS)
    frame = _signals(rows)
    # 12 月给 5 个观测、1 月末累计 10 个。门槛 8 ⇒ 1 月仍不足、2 月才够。
    params = BacktestParams(
        ema_short=2, ema_long=3, tnr_window=2, atr_window=2, dtnr_k=2, min_observations=8
    )

    result = run_backtest(frame, params=params, signals=frame)
    before = result.positions.loc[result.positions["trade_date"] < date(2024, 2, 1)]
    after = result.positions.loc[result.positions["trade_date"] >= date(2024, 2, 1)]

    assert not before.empty
    assert (before["weight"] == 0.0).all()
    assert (after["weight"].abs() > 0).all()


def test_costs_scale_linearly_with_traded_notional():
    """成本按**成交名义额**收，1.3 个基点。"""
    rows = _series(product="RB", contract="RB2405.SHF", days=ALL_DAYS)
    rows += _series(product="HC", contract="HC2405.SHF", days=MONTH2, close=2000.0)
    frame = _signals(rows)

    result = run_backtest(frame, params=FAST, signals=frame)
    executions = result.executions

    assert not executions.empty
    residual = executions["cost"] - executions["turnover"] * FAST.cost_bps / 10_000.0
    assert residual.abs().max() == pytest.approx(0.0, abs=1e-15)
    assert result.daily["direct_cost"].sum() == pytest.approx(executions["cost"].sum())


def test_the_volatility_multiplier_reads_a_shadow_that_excludes_itself():
    """D17：`Mul_vol` 的输入是**只加 ATR 杠杆**的影子收益。

    按字面读研报，第一年 `Lev = 0` ⇒ 收益恒 0 ⇒ 标准差 0 ⇒ `final_leverage` 对零
    波动硬失败，策略永远启动不了。影子序列是唯一能自举的读法，也是本仓股指侧
    （`index_open_momentum/run.py`）已经用的那条。
    """
    rows = _series(product="RB", contract="RB2405.SHF", days=ALL_DAYS)
    rows += _series(product="HC", contract="HC2405.SHF", days=MONTH2, close=2000.0)
    frame = _signals(rows)

    result = run_backtest(frame, params=FAST, signals=frame)

    assert len(result.shadow_daily) == len(result.daily)
    assert not result.shadow_daily["net_return"].equals(result.daily["net_return"])
    # 影子的杠杆恒为 Lev_ATR，与波动率无关，所以它的换手与本体不同。
    assert result.shadow_daily["gross_leverage"].max() > 0.0


def test_an_unpriceable_fill_defers_the_trade_instead_of_inventing_a_price():
    """D19：拿不到成交价就顺延到下一个有价的槽，并记账 —— 不许就地换一个价。"""
    rows = _series(product="RB", contract="RB2405.SHF", days=ALL_DAYS)
    control = _signals(rows)
    blinded = _series(product="RB", contract="RB2405.SHF", days=ALL_DAYS)
    for row in blinded:
        if row["trade_date"] == MONTH2[1]:
            row["fill_price"] = None
            row["fill_unpriceable"] = True
    frame = _signals(blinded)

    result = run_backtest(frame, params=FAST, signals=frame)
    baseline = run_backtest(control, params=FAST, signals=control)

    assert not result.deferred_fills.empty
    assert set(result.deferred_fills["product"]) == {"RB"}
    assert set(result.deferred_fills["trade_date"].dt.date) == {MONTH2[1]}
    # 记了账还不够 —— 那几笔**真的没有成交**。与对照跑比，成交笔数必须变少。
    assert len(result.executions) < len(baseline.executions)
    assert result.executions["price"].notna().all()
    assert (result.executions["price"] > 0).all()


def test_the_backtest_refuses_a_panel_whose_columns_it_does_not_know():
    frame = _price_panel(_zigzag(14)).drop(columns=["fill_price"])

    with pytest.raises(ValueError) as caught:
        run_backtest(frame, params=FAST)

    assert str(caught.value).startswith("backtest_panel_columns:")


def test_the_dtnr_mode_actually_reaches_the_indicator_layer():
    """记下 `dtnr_mode` 不等于用上它。

    只断言「摘要里写着 lag」是抓不到「参数根本没传下去」的 —— 两条口径必须在同一条
    价格路径上给出**不同的信号序列**。
    """
    closes = _zigzag(20)
    mean = replace(SIGNAL_PARAMS, dtnr_mode="mean")
    lag = replace(SIGNAL_PARAMS, dtnr_mode="lag")

    a = product_signals(_price_panel(closes), params=mean)
    b = product_signals(_price_panel(closes), params=lag)

    assert not a["delta_tnr"].equals(b["delta_tnr"])
    # ⚠️ 不能顺手断言「方向也不同」：本夹具的 TNR 严格上升，两种口径的 ΔTNR **符号
    # 相同**，闸自然开在同几根上。真正的接线判据是 ΔTNR 这一列本身 —— 模式没传下去
    # 时它会一模一样（变异验证过）。
    assert a["direction"].equals(b["direction"])


def test_the_exit_rule_actually_reaches_the_signal_layer():
    """记下 `exit_gates` 不等于用上它 —— 两条读法必须给出不同的方向序列。"""
    closes = _zigzag(20)
    wide = product_signals(_price_panel(closes), params=replace(SIGNAL_PARAMS, exit_gates="wide"))
    narrow = product_signals(_price_panel(closes), params=replace(SIGNAL_PARAMS, exit_gates="narrow"))

    assert list(wide["direction"]) != list(narrow["direction"])
    # 窄口径只放宽**离场**，所以它持仓的 bar 不会比宽口径少。
    assert (narrow["direction"] != "flat").sum() >= (wide["direction"] != "flat").sum()


def test_narrow_exit_does_not_open_before_warmup():
    """预热未完成的那几根被喂成「永不开仓」的输入，状态机不该以为它们持过仓。"""
    closes = _zigzag(20)
    narrow = product_signals(_price_panel(closes), params=replace(SIGNAL_PARAMS, exit_gates="narrow"))

    assert set(narrow.loc[~narrow["warm"], "direction"]) <= {"flat"}


def test_backtest_params_rejects_an_exit_rule_it_does_not_know():
    with pytest.raises(ValueError) as caught:
        BacktestParams(exit_gates="trailing")
    assert str(caught.value).startswith("backtest_params: exit_gates")


def test_the_lag_mode_needs_one_more_bar_of_warmup():
    """滞后版 `TNR_t − TNR_{t−k}` 在第 k+1 个 TNR 上才有定义，均值版第 k 个就有。"""
    mean = BacktestParams(ema_short=2, ema_long=3, tnr_window=2, atr_window=2, dtnr_k=2)
    lag = replace(mean, dtnr_mode="lag")

    assert mean.warmup_bars == 3
    assert lag.warmup_bars == 4


def test_backtest_params_rejects_a_dtnr_mode_it_does_not_know():
    with pytest.raises(ValueError) as caught:
        BacktestParams(dtnr_mode="ewm")
    assert str(caught.value).startswith("backtest_params: dtnr_mode")


# --- D22：调仓节拍 -----------------------------------------------------------

def _rebalance_result(cadence, days=None):
    rows = _series(product="RB", contract="RB2405.SHF", days=days or ALL_DAYS)
    rows += _series(product="HC", contract="HC2405.SHF", days=days or ALL_DAYS, close=2000.0)
    frame = _signals(rows)
    params = replace(FAST, rebalance=cadence)
    return frame, run_backtest(frame, params=params, signals=frame)


def test_slot_cadence_is_the_current_reading_and_trades_the_most():
    """`rebalance="slot"` 是现状：每个事件都把权重重算一遍。"""
    _, slot = _rebalance_result("slot")
    _, entry = _rebalance_result("entry")

    assert slot.daily["turnover"].sum() > entry.daily["turnover"].sum()


def test_entry_cadence_only_trades_on_entries_exits_and_rolls():
    """仓位在**开仓时点**定死，之后不再随信号强弱与杠杆漂移改动。

    这正是研报 §1.6 那一档的措辞 —— 「+ 开仓时点信号强弱」。
    """
    _, result = _rebalance_result("entry")

    assert not result.executions.empty
    assert set(result.executions["cause"]) <= {
        TurnoverCause.SIGNAL, TurnoverCause.UNIVERSE, TurnoverCause.ROLL
    }
    # 再平衡那一类必须一笔都没有 —— `turnover_by_cause` 是原始 groupby，没发生的
    # 成因根本不会出现（补零留行是报告层 `report.turnover_breakdown` 的事）。
    assert TurnoverCause.REBALANCE not in set(result.turnover_by_cause["cause"])


def test_cadences_order_by_how_often_they_let_weights_move():
    """slot ≥ daily ≥ monthly ≥ entry —— 节拍越粗，换手越低。"""
    results = {
        cadence: _rebalance_result(cadence)[1]
        for cadence in ("slot", "daily", "monthly", "entry")
    }
    turnovers = {k: v.daily["turnover"].sum() for k, v in results.items()}
    trades = {k: len(v.executions) for k, v in results.items()}

    # ⚠️ 必须是**严格**大于：写成 >= 的话，「日内后续事件也放行」这种把 daily 退化成
    # slot 的变异照样通过 —— 那条一开始就没被抓住。
    assert turnovers["slot"] > turnovers["daily"] > turnovers["monthly"] > turnovers["entry"]
    assert trades["slot"] > trades["daily"] > trades["monthly"] > trades["entry"]


def test_a_coarse_cadence_still_lets_a_signal_change_trade():
    """收窄的是**再平衡**，不是开平仓。信号翻转任何节拍下都必须成交。"""
    _, result = _rebalance_result("monthly")
    causes = set(result.executions["cause"])

    assert TurnoverCause.SIGNAL in causes


def test_backtest_params_rejects_a_cadence_it_does_not_know():
    with pytest.raises(ValueError) as caught:
        BacktestParams(rebalance="hourly")
    assert str(caught.value).startswith("backtest_params: rebalance")
