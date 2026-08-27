"""日内与隔夜持仓路径（计划 Task 5）。

研报口径：

- 开盘信号确认后建仓（成交价 = 信号后 5 分钟 VWAP）；
- 每次止损事件减掉**初始仓位的三分之一**，第三次减完即平；
- 多头信号且未被完全止损 → 剩余仓位**持隔夜**，次日开盘平；
- 空头信号 → 当日收盘平，不留隔夜；
- 成本 1.3%%（单边 0.00013）。

成交价用可注入的 `fill_price(bar_index)` 表达 —— 生产上它是分钟层的 5 分钟 VWAP
（Task 2/7，被 Task 0 裁决 A 阻塞），测试里是确定性字典。这样本模块的路径逻辑不必
等分钟层就能验完。
"""

import pytest

from index_open_momentum.backtest import FillKind, simulate_session
from index_open_momentum.types import Bar

# 前三根 open/low/close 严格递增 → 多头信号
_LONG_BARS = [
    Bar(open=4000.0, high=4010.0, low=3995.0, close=4005.0),
    Bar(open=4006.0, high=4020.0, low=4001.0, close=4015.0),
    Bar(open=4016.0, high=4030.0, low=4011.0, close=4025.0),
    Bar(open=4026.0, high=4040.0, low=4021.0, close=4035.0),
    Bar(open=4036.0, high=4050.0, low=4031.0, close=4045.0),
]
_ATR = [None, None, None, 20.0, 20.0]
_ENTRY_AT_4000 = {2: 4000.0}


def test_a_neutral_opening_leaves_no_position_at_all():
    flat = [Bar(open=4000.0, high=4010.0, low=3995.0, close=4005.0)] * 5
    result = simulate_session(
        bars=flat, atr_at=_ATR, fill_price=lambda i: 4000.0, next_session_open=4100.0
    )
    assert result.direction is None
    assert result.fills == ()
    assert result.net_return == 0.0


def test_an_unstopped_long_is_carried_overnight_and_closed_at_the_next_open():
    result = simulate_session(
        bars=_LONG_BARS,
        atr_at=_ATR,
        fill_price=lambda i: _ENTRY_AT_4000[i],
        next_session_open=4040.0,
    )
    kinds = [f.kind for f in result.fills]
    assert kinds == [FillKind.ENTRY, FillKind.OVERNIGHT_EXIT]
    assert result.carried_overnight == pytest.approx(1.0)
    # 建仓 4000 → 次日开盘 4040：(4040-4000)/4000 = 0.01
    assert result.gross_return == pytest.approx(0.01)
    # 单边 0.00013，建仓 1.0 + 平仓 1.0 = 2.0 手仓位换手
    assert result.cost == pytest.approx(0.00026)
    assert result.net_return == pytest.approx(0.01 - 0.00026)


# 前三根 open/high/close 严格递减 → 空头信号；后两根不触发任何止损
_SHORT_BARS = [
    Bar(open=4025.0, high=4030.0, low=4011.0, close=4016.0),
    Bar(open=4015.0, high=4020.0, low=4001.0, close=4006.0),
    Bar(open=4005.0, high=4010.0, low=3995.0, close=4000.0),
    Bar(open=3999.0, high=4005.0, low=3990.0, close=3995.0),
    Bar(open=3994.0, high=4000.0, low=3985.0, close=3990.0),
]


def test_a_short_is_flattened_at_the_day_close_and_never_carried():
    result = simulate_session(
        bars=_SHORT_BARS,
        atr_at=_ATR,
        fill_price=lambda i: 4000.0,
        next_session_open=3900.0,  # 空头不该碰它
    )
    assert [f.kind for f in result.fills] == [FillKind.ENTRY, FillKind.DAY_CLOSE_EXIT]
    assert result.carried_overnight == 0.0
    # 建仓 4000 → 收盘 3990，空头赚 10 点：+(4000-3990)/4000 = 0.0025
    assert result.gross_return == pytest.approx(0.0025)
    assert result.net_return == pytest.approx(0.0025 - 0.00026)


_LONG_THEN_BREAK = [
    *_LONG_BARS[:4],  # 前四根同多头样本；bar3 把入场后最高价定在 4040
    Bar(open=4030.0, high=4032.0, low=3960.0, close=3970.0),  # 收盘 3970 < 4040-2.5*20=3990
    Bar(open=3970.0, high=3975.0, low=3950.0, close=3955.0),
    Bar(open=3955.0, high=3960.0, low=3940.0, close=3945.0),
    # ⚠️ 收尾 bar：复刻假设⑪规定**当日最后一根不判减仓**（其后没有 5 分钟成交窗）。
    # 补这一根，是为了让上面几根仍然是"中途"的 bar，被测的减仓机制不受影响。
    Bar(open=3945.0, high=3948.0, low=3942.0, close=3946.0),
]
_ATR_7 = [None, None, None, 20.0, 20.0, 20.0, 20.0, 20.0]


def test_one_stop_takes_out_a_third_and_the_rest_still_goes_overnight():
    prices = {2: 4000.0, 4: 3965.0}
    result = simulate_session(
        bars=_LONG_THEN_BREAK[:6],
        atr_at=_ATR_7[:6],
        fill_price=lambda i: prices[i],
        next_session_open=3980.0,
    )
    assert [f.kind for f in result.fills] == [
        FillKind.ENTRY, FillKind.SCALE_DOWN, FillKind.OVERNIGHT_EXIT
    ]
    assert result.fills[1].size == pytest.approx(1 / 3)
    assert result.carried_overnight == pytest.approx(2 / 3)
    # 减仓 1/3 @3965：(3965-4000)/4000/3 = -0.0029166667
    # 隔夜 2/3 @3980：(3980-4000)/4000*2/3 = -0.0033333333
    assert result.gross_return == pytest.approx(-0.00625)
    assert result.cost == pytest.approx(0.00026)  # 换手仍是 1.0 进 + 1.0 出


def test_the_third_stop_flattens_and_nothing_is_carried():
    prices = {2: 4000.0, 4: 3965.0, 5: 3960.0, 6: 3950.0}
    result = simulate_session(
        bars=_LONG_THEN_BREAK,
        atr_at=_ATR_7,
        fill_price=lambda i: prices[i],
        next_session_open=3980.0,
    )
    assert [f.kind for f in result.fills] == [
        FillKind.ENTRY, FillKind.SCALE_DOWN, FillKind.SCALE_DOWN, FillKind.SCALE_DOWN
    ]
    assert result.carried_overnight == 0.0
    # 三次各 1/3：(-35 + -40 + -50)/4000/3 = -125/12000
    assert result.gross_return == pytest.approx(-125 / 12000)


_BOTH_FAMILIES = [
    Bar(open=4000.0, high=4100.0, low=3995.0, close=4005.0),
    Bar(open=4006.0, high=4090.0, low=4001.0, close=4015.0),
    Bar(open=4016.0, high=4080.0, low=4011.0, close=4025.0),
    Bar(open=4026.0, high=4070.0, low=4021.0, close=4035.0),
    Bar(open=4036.0, high=4060.0, low=4031.0, close=3900.0),
    # 收尾 bar，同上：让第 4 根仍是"中途"的（复刻假设⑪）。
    Bar(open=3900.0, high=3905.0, low=3898.0, close=3902.0),
]
# open/low/close 前三根严格递增 → 多头；同时 high 4100>4090>4080>4070>4060 五根严格递减
# 第 4 根（index 4）：反向止损成立，且收盘 3900 < 入场后最高 4070 - 2.5*20 = 4020 → 吊灯也成立


def test_two_families_firing_on_one_bar_still_cuts_only_one_third():
    result = simulate_session(
        bars=_BOTH_FAMILIES,
        atr_at=_ATR,
        fill_price=lambda i: 4000.0,
        next_session_open=4000.0,
    )
    scale_downs = [f for f in result.fills if f.kind is FillKind.SCALE_DOWN]
    assert len(scale_downs) == 1
    assert scale_downs[0].bar_index == 4
    assert scale_downs[0].size == pytest.approx(1 / 3)
    assert scale_downs[0].stop is not None
    assert len(scale_downs[0].stop.triggered) == 2  # 两族都响了，仍只减一档
    assert result.carried_overnight == pytest.approx(2 / 3)


def test_a_surviving_long_without_a_next_open_is_a_hard_failure():
    """没有次日开盘价就无法给隔夜仓计价 —— 不许静默按收盘价代替。"""
    with pytest.raises(ValueError, match="next_session_open"):
        simulate_session(
            bars=_LONG_BARS,
            atr_at=_ATR,
            fill_price=lambda i: 4000.0,
            next_session_open=None,
        )


def test_a_flat_position_takes_no_further_stops_for_the_rest_of_the_day():
    """第三档减完即平，当日不再建仓 —— 后面的 bar 再怎么跌也不该产生第四次减仓。"""
    bars = [*_LONG_THEN_BREAK, Bar(open=3945.0, high=3950.0, low=3900.0, close=3905.0)]
    prices = {2: 4000.0, 4: 3965.0, 5: 3960.0, 6: 3950.0, 7: 3910.0}
    result = simulate_session(
        bars=bars,
        atr_at=[*_ATR_7, 20.0],
        fill_price=lambda i: prices[i],
        next_session_open=3980.0,
    )
    assert len([f for f in result.fills if f.kind is FillKind.SCALE_DOWN]) == 3
    assert result.carried_overnight == 0.0
    # 多出来的那根 bar 一分钱也不该影响损益
    assert result.gross_return == pytest.approx(-125 / 12000)


def test_the_opening_bars_own_extremes_do_not_belong_to_the_position():
    """入场后极值从建仓那根之后起算 —— 开盘三根里的高点不属于这笔持仓。

    第 0 根的 high 是 4200，若把它算进"入场后最高价"，第 3 根就会因
    `4035 < 4200 - 2.5*20 = 4150` 触发吊灯止损。按正确口径（入场后最高价
    只有 4040 / 4045），阈值是 3990 / 3995，全程不触发。
    """
    bars = [
        Bar(open=4000.0, high=4200.0, low=3995.0, close=4005.0),
        Bar(open=4006.0, high=4020.0, low=4001.0, close=4015.0),
        Bar(open=4016.0, high=4030.0, low=4011.0, close=4025.0),
        Bar(open=4026.0, high=4040.0, low=4021.0, close=4035.0),
        Bar(open=4036.0, high=4045.0, low=4031.0, close=4100.0),
    ]
    result = simulate_session(
        bars=bars, atr_at=_ATR, fill_price=lambda i: 4000.0, next_session_open=4100.0
    )
    assert [f.kind for f in result.fills] == [FillKind.ENTRY, FillKind.OVERNIGHT_EXIT]
    assert result.carried_overnight == pytest.approx(1.0)


# --------------------------------------------------------------------------
# 当日最后一根不判减仓
# --------------------------------------------------------------------------


def test_no_scale_down_is_taken_on_the_final_bar_of_the_session():
    """⚠️ 复刻假设⑪：最后一根之后**没有**5 分钟成交窗，减仓无从计价。

    研报的减仓一律按"信号后 5 分钟 VWAP"成交；当日最后一根没有那个窗口。
    而剩余仓位本来就会被日末规则（空头收盘平 / 多头留隔夜）处理掉，所以在
    最后一根上再减一档是多余的，且只能拿一个**不存在**的价去成交。

    这条是端到端真跑炸出来的：2016-01-11 的 IF 主力在第 15 根（当日最后一根）
    触发了吊灯止损。
    """
    from index_open_momentum.backtest import FillKind, simulate_session
    from index_open_momentum.types import Bar

    bars = [
        Bar(open=4000.0 + i, high=4005.0 + i, low=3999.0 + i, close=4002.0 + i)
        for i in range(3)
    ]
    # 最后一根暴跌，足以击穿吊灯
    bars.append(Bar(open=4003.0, high=4004.0, low=3000.0, close=3000.0))
    atr = [None, None, 10.0, 10.0]

    asked: list[int] = []

    def fill_price(index):
        asked.append(index)
        return bars[index].close

    result = simulate_session(
        bars=bars, atr_at=atr, fill_price=fill_price, next_session_open=4100.0
    )

    assert [f.kind for f in result.fills if f.kind is FillKind.SCALE_DOWN] == []
    assert 3 not in asked  # 从没向最后一根要过成交价
