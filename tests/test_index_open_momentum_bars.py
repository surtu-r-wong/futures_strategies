"""15 分钟 K 线与 5 分钟 VWAP —— 计划 Task 2。

本模块是**薄封装**：聚合、乘数反推、VWAP 与价域校验全部复用 `common/minute/bars.py`。
所以这里测的不是那些算法本身（它们有自己的测试家族），而是**接缝**：

1. 15 分钟 K 线由 `fifteen_minute_buckets` 发的桶驱动 ⇒ 结构上不可能跨休市拼接；
2. 中金所侧把成交价越界容差**收紧到 1e-6**（共享层的 1e-4 是给商品涨停锁死 bar 的）；
3. 乘数「先元数据、缺失才反推」的分流；
4. no-trade bar 不更新吊灯极值、不触发止损、并**打断**反向信号的连续计数。

⚠️ 2026-08-27 实测（IF/IC/IH 主力，2012/2015/2018/2024 四年）：
- 2,139 个真实执行窗口的 VWAP 相对越界**最大值 = 0.0**，含一个涨停锁死窗口 ⇒ 1e-6 安全；
- ⚠️ 曾据四年抽样得出"整桶零成交从未出现"，**已被端到端真跑推翻**：2016 Q1 的
  IF 主力 58 个 product-day 里有 **14 根**。no-trade 是**常规路径**。
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from common.minute.bars import MinuteDataError
from common.minute.sessions import (
    build_trading_slots,
    fifteen_minute_buckets,
    resolve_session_rule,
)
from index_open_momentum.backtest import FillKind, simulate_session
from index_open_momentum.bars import (
    MAX_RELATIVE_EXCURSION,
    IndexBar,
    build_index_bars,
    index_execution_fill,
    relative_excursion,
    resolve_index_multiplier,
)
from index_open_momentum.risk import Direction
from index_open_momentum.sessions import load_index_session_rules
from index_open_momentum.types import Bar

SHANGHAI = ZoneInfo("Asia/Shanghai")
IF_MULTIPLIER = 300


def _rule(product, trade_date):
    return resolve_session_rule(
        load_index_session_rules(), "CFFEX", product, trade_date
    )


def _buckets(product, trade_date, previous):
    rule = _rule(product, trade_date)
    return fifteen_minute_buckets(build_trading_slots(trade_date, previous, rule), rule)


def _frame(contract, slots, prices, volumes, *, multiplier=IF_MULTIPLIER, amounts=None):
    """按给定 slot 造分钟行；`amount` 默认取 price × volume × multiplier。"""
    records = []
    for index, slot in enumerate(slots):
        price = prices[index]
        volume = volumes[index]
        records.append(
            {
                "bar_time": slot,
                "symbol": contract,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "amount": (
                    amounts[index]
                    if amounts is not None
                    else price * volume * multiplier
                ),
            }
        )
    return pd.DataFrame(records)


# --------------------------------------------------------------------------
# 1. 15 分钟 K 线：由时段桶驱动
# --------------------------------------------------------------------------


def test_a_late_era_day_yields_sixteen_bars():
    """240 官方分钟 / 15 = 16 根。"""
    buckets = _buckets("IF", date(2016, 6, 1), date(2016, 5, 31))

    assert len(buckets) == 16


def test_an_early_era_day_yields_eighteen_bars():
    """270 官方分钟 / 15 = 18 根 —— 年代不同，一天的 bar 数就不同。"""
    buckets = _buckets("IF", date(2011, 6, 1), date(2011, 5, 31))

    assert len(buckets) == 18


def test_no_bar_straddles_the_lunch_break():
    """每根 bar 的 15 个 slot 必须首尾相差正好 14 分钟 —— 跨休市会立刻超出。"""
    buckets = _buckets("IF", date(2016, 6, 1), date(2016, 5, 31))

    for bucket in buckets:
        assert bucket[-1] - bucket[0] == timedelta(minutes=14)


def test_a_bar_aggregates_open_high_low_close_from_traded_minutes():
    buckets = _buckets("IF", date(2016, 6, 1), date(2016, 5, 31))
    slots = buckets[0]
    prices = [4000.0 + i for i in range(15)]  # 4000 递增到 4014
    frame = _frame("IF1606", slots, prices, [10.0] * 15)

    bars = build_index_bars(frame, buckets=buckets[:1], contract="IF1606")

    assert len(bars) == 1
    assert bars[0].bar == Bar(open=4000.0, high=4014.0, low=4000.0, close=4014.0)
    assert bars[0].no_trade is False
    assert bars[0].volume == 150.0


def test_zero_volume_minutes_do_not_enter_the_bar_extremes():
    """一根 0 成交的分钟报了个离谱价，不许污染这根 15 分钟 K 线的高低点。"""
    buckets = _buckets("IF", date(2016, 6, 1), date(2016, 5, 31))
    slots = buckets[0]
    prices = [4000.0] * 15
    prices[7] = 1.0
    volumes = [10.0] * 15
    volumes[7] = 0.0
    frame = _frame("IF1606", slots, prices, volumes)

    bars = build_index_bars(frame, buckets=buckets[:1], contract="IF1606")

    assert bars[0].bar.low == 4000.0


def test_a_bucket_with_no_traded_volume_is_a_no_trade_bar():
    buckets = _buckets("IF", date(2016, 6, 1), date(2016, 5, 31))
    frame = _frame("IF1606", buckets[0], [4000.0] * 15, [0.0] * 15)

    bars = build_index_bars(frame, buckets=buckets[:1], contract="IF1606")

    assert bars[0].no_trade is True
    assert bars[0].bar is None
    assert bars[0].volume == 0.0


def test_bars_come_back_in_session_order():
    buckets = _buckets("IF", date(2016, 6, 1), date(2016, 5, 31))
    slots = [slot for bucket in buckets[:3] for slot in bucket]
    frame = _frame("IF1606", slots, [4000.0] * 45, [10.0] * 45)

    bars = build_index_bars(frame, buckets=buckets[:3], contract="IF1606")

    assert [b.start for b in bars] == [bucket[0] for bucket in buckets[:3]]
    assert all(isinstance(b, IndexBar) for b in bars)


# --------------------------------------------------------------------------
# 2. 5 分钟 VWAP 与收紧的价域闸
# --------------------------------------------------------------------------


def _fill_slots(product="IF", trade_date=date(2016, 6, 1)):
    """晚年代的执行窗：前 3 根 bar 收在 10:15，其后 5 分钟即 10:15–10:19。"""
    buckets = _buckets(product, trade_date, date(2016, 5, 31))
    return buckets[3][:5]


def test_vwap_is_amount_over_volume_over_multiplier():
    slots = _fill_slots()
    prices = [4000.0, 4001.0, 4002.0, 4003.0, 4004.0]
    frame = _frame("IF1606", slots, prices, [10.0] * 5)

    fill = index_execution_fill(
        frame, slots=slots, contract="IF1606", multiplier=IF_MULTIPLIER
    )

    # sum(amount) = 300×10×(4000+4001+4002+4003+4004) = 60,030,000
    # sum(volume) = 50 ⇒ 60,030,000 / 50 / 300 = 4002.0
    assert fill.price == pytest.approx(4002.0)
    assert fill.volume == 50.0


def test_only_traded_minutes_enter_the_vwap():
    """0 成交的分钟带着一个假价进来，不许把 VWAP 拉走。"""
    slots = _fill_slots()
    prices = [4000.0, 1.0, 4000.0, 4000.0, 4000.0]
    volumes = [10.0, 0.0, 10.0, 10.0, 10.0]
    frame = _frame("IF1606", slots, prices, volumes)

    fill = index_execution_fill(
        frame, slots=slots, contract="IF1606", multiplier=IF_MULTIPLIER
    )

    assert fill.price == pytest.approx(4000.0)
    assert fill.volume == 40.0


def test_a_window_with_zero_total_volume_is_a_hard_failure():
    """必需的成交窗零成交 ⇒ 硬失败，不顺延到更晚的窗口。"""
    slots = _fill_slots()
    frame = _frame("IF1606", slots, [4000.0] * 5, [0.0] * 5)

    with pytest.raises(MinuteDataError, match="execution_vwap"):
        index_execution_fill(
            frame, slots=slots, contract="IF1606", multiplier=IF_MULTIPLIER
        )


def test_the_window_must_be_exactly_five_slots():
    slots = _fill_slots()[:4]
    frame = _frame("IF1606", slots, [4000.0] * 4, [10.0] * 4)

    with pytest.raises(MinuteDataError):
        index_execution_fill(
            frame, slots=slots, contract="IF1606", multiplier=IF_MULTIPLIER
        )


def test_the_index_gate_is_tighter_than_the_shared_layer():
    """中金所 amount 是真成交额 ⇒ 容差 1e-6，不是共享层给商品涨停 bar 的 1e-4。"""
    assert MAX_RELATIVE_EXCURSION == 1e-6


def test_a_vwap_outside_a_real_price_range_is_a_hard_failure():
    """有真实价差的窗口里 VWAP 越界 ⇒ amount 与 OHLC 对不上，硬失败。"""
    slots = _fill_slots()
    prices = [4000.0, 4001.0, 4002.0, 4003.0, 4004.0]
    # amount 整体放大 0.1%：raw ≈ 4006.0，高出 high=4004 约 2 个点
    amounts = [p * 10.0 * IF_MULTIPLIER * 1.001 for p in prices]
    frame = _frame("IF1606", slots, prices, [10.0] * 5, amounts=amounts)

    with pytest.raises(MinuteDataError, match="execution_vwap"):
        index_execution_fill(
            frame, slots=slots, contract="IF1606", multiplier=IF_MULTIPLIER
        )


def test_a_tiny_excursion_inside_the_index_tolerance_is_accepted():
    slots = _fill_slots()
    prices = [4000.0, 4001.0, 4002.0, 4003.0, 4004.0]
    frame = _frame("IF1606", slots, prices, [10.0] * 5)

    fill = index_execution_fill(
        frame, slots=slots, contract="IF1606", multiplier=IF_MULTIPLIER
    )

    assert relative_excursion(fill) == pytest.approx(0.0, abs=1e-12)


def test_a_zero_width_window_is_exempt_from_the_tight_gate():
    """low == high 时窗口只可能成交在这一个价上，越界**只能**是 amount 的取整残差。

    这正是共享层 `_fill_epsilon` 那段注释的论证，本层照单接受 —— 但 `relative_excursion`
    仍如实报出残差，好让保真度报告看得见它。
    """
    slots = _fill_slots()
    prices = [4000.0] * 5
    # raw = 4000.01 ⇒ 相对越界 2.5e-6，超过 1e-6，但窗口零宽
    amounts = [4000.01 * 10.0 * IF_MULTIPLIER] * 5
    frame = _frame("IF1606", slots, prices, [10.0] * 5, amounts=amounts)

    fill = index_execution_fill(
        frame, slots=slots, contract="IF1606", multiplier=IF_MULTIPLIER
    )

    assert fill.price == 4000.0
    assert relative_excursion(fill) == pytest.approx(2.5e-6, rel=1e-3)


def test_the_index_tolerance_can_be_loosened_within_the_shared_bound():
    """放宽只在共享层允许的范围内生效。raw = 4004.2 ⇒ 相对越界 5.0e-5，介于两道闸之间。"""
    slots = _fill_slots()
    prices = [4000.0, 4001.0, 4002.0, 4003.0, 4004.0]
    amounts = [p * 10.0 * IF_MULTIPLIER * (4004.2 / 4002.0) for p in prices]
    frame = _frame("IF1606", slots, prices, [10.0] * 5, amounts=amounts)

    with pytest.raises(MinuteDataError, match="execution_vwap"):
        index_execution_fill(
            frame, slots=slots, contract="IF1606", multiplier=IF_MULTIPLIER
        )

    fill = index_execution_fill(
        frame,
        slots=slots,
        contract="IF1606",
        multiplier=IF_MULTIPLIER,
        max_relative_excursion=1e-3,
    )

    assert relative_excursion(fill) == pytest.approx(5.0e-5, rel=1e-2)
    assert fill.price == 4004.0  # 共享层把越界价夹回窗口内成交过的最近价


def test_the_index_tolerance_cannot_be_loosened_past_the_shared_bound():
    """本层的闸只能**更紧**。共享层 1e-4 那道是外界，放宽参数越不过去。

    这不是缺陷：越过 1e-4 的越界在商品侧已经被认定为"turnover 与 OHLC 真的对不上"，
    没有哪个市场该把它当成取整残差放行。
    """
    slots = _fill_slots()
    prices = [4000.0, 4001.0, 4002.0, 4003.0, 4004.0]
    amounts = [p * 10.0 * IF_MULTIPLIER * 1.001 for p in prices]  # raw ≈ 4006.0
    frame = _frame("IF1606", slots, prices, [10.0] * 5, amounts=amounts)

    with pytest.raises(MinuteDataError, match="VWAP is outside the traded price range"):
        index_execution_fill(
            frame,
            slots=slots,
            contract="IF1606",
            multiplier=IF_MULTIPLIER,
            max_relative_excursion=1.0,
        )


# --------------------------------------------------------------------------
# 3. 合约乘数：先元数据、缺失才反推
# --------------------------------------------------------------------------


def _multiplier_frame(contract, multiplier, *, days=6):
    """跨多日的分钟行，够 `_select_multiplier_sample` 抽样。"""
    records = []
    for day in range(days):
        trade_date = date(2016, 6, 1) + timedelta(days=day)
        start = datetime(
            trade_date.year, trade_date.month, trade_date.day, 9, 30, tzinfo=SHANGHAI
        )
        for minute in range(30):
            price = 4000.0 + minute
            records.append(
                {
                    "bar_time": start + timedelta(minutes=minute),
                    "symbol": contract,
                    "trade_date": trade_date,
                    "open": price,
                    "high": price + 0.2,
                    "low": price - 0.2,
                    "close": price,
                    "volume": 100.0,
                    "amount": price * 100.0 * multiplier,
                }
            )
    return pd.DataFrame(records)


def test_metadata_multiplier_is_used_when_present():
    frame = _multiplier_frame("IF1606", IF_MULTIPLIER)

    resolution = resolve_index_multiplier(
        frame, contract="IF1606", metadata_multiplier=IF_MULTIPLIER
    )

    assert resolution.multiplier == IF_MULTIPLIER
    assert resolution.source == "metadata"


def test_the_multiplier_is_inferred_when_metadata_is_missing():
    """`futures_contract_info` 只覆盖 2025-12-22 起 ⇒ 回测窗口绝大部分走这条分支。"""
    frame = _multiplier_frame("IC1606", 200)

    resolution = resolve_index_multiplier(frame, contract="IC1606")

    assert resolution.multiplier == 200
    assert resolution.source == "inferred"


def test_metadata_that_contradicts_the_bars_is_a_hard_failure():
    """元数据说 300、数据说 200 ⇒ 不许静默采信任何一边。"""
    frame = _multiplier_frame("IC1606", 200)

    with pytest.raises(MinuteDataError, match="metadata_multiplier"):
        resolve_index_multiplier(frame, contract="IC1606", metadata_multiplier=300)


# --------------------------------------------------------------------------
# 4. no-trade bar 在持仓路径里的语义
# --------------------------------------------------------------------------
#
# `simulate_session` 收 `Sequence[Bar | None]`，`None` 就是"这根没成交"。
# 不用平行的布尔数组：`IndexBar.bar` 无成交时本来就是 None，用数组的话调用方
# 得**伪造**一根 Bar 才能填进序列 —— 那正是 `types.Bar` 反对的事，而且两个
# 序列还能对不齐。


def _rising(n, base=4000.0):
    """n 根严格递增的 bar —— 前 3 根构成多头开盘信号。"""
    return [
        Bar(open=base + i, high=base + i + 5, low=base + i - 1, close=base + i + 2)
        for i in range(n)
    ]


def test_a_no_trade_bar_among_the_opening_three_means_no_position():
    """开盘三根里缺一根就没有判据 —— 当日不交易，不是"用剩下两根凑"。"""
    bars = _rising(6)
    bars[1] = None

    result = simulate_session(
        bars=bars,
        atr_at=[None, None, None, 10.0, 10.0, 10.0],
        fill_price=lambda i: bars[i].close,
        next_session_open=4100.0,
    )

    assert result.direction is None
    assert result.fills == ()


def test_a_no_trade_bar_does_not_trigger_a_stop():
    """吊灯阈值早被击穿，但那根 bar 没有成交 ⇒ 不许在它上面减仓。"""
    bars = _rising(6)
    bars[3] = None
    atr = [None, None, None, 10.0, 10.0, 10.0]

    result = simulate_session(
        bars=bars,
        atr_at=atr,
        fill_price=lambda i: bars[i].close,
        next_session_open=4100.0,
    )

    assert [f.bar_index for f in result.fills if f.kind is FillKind.SCALE_DOWN] == []


def test_a_no_trade_bar_does_not_update_the_chandelier_extreme():
    """no-trade bar 的高低价不存在，不许被计进入场后极值。

    对照组把同一位置换成一根真有成交、high=9999 的 bar：那时第 5 根会被
    9999 − 2.5×ATR 击穿而减仓。两组只差这一根的成交与否。
    """
    traded = _rising(6)
    traded[3] = Bar(open=4003.0, high=9999.0, low=4000.0, close=4005.0)
    atr = [None, None, None, 10.0, 10.0, 10.0]

    control = simulate_session(
        bars=traded,
        atr_at=atr,
        fill_price=lambda i: traded[i].close,
        next_session_open=4100.0,
    )

    absent = list(traded)
    absent[3] = None
    treatment = simulate_session(
        bars=absent,
        atr_at=atr,
        fill_price=lambda i: absent[i].close,
        next_session_open=4100.0,
    )

    assert [f.bar_index for f in control.fills if f.kind is FillKind.SCALE_DOWN] != []
    assert [f.bar_index for f in treatment.fills if f.kind is FillKind.SCALE_DOWN] == []


def test_a_no_trade_bar_breaks_the_reverse_stop_run():
    """多头反向止损要 5 根**连续**递减高点。中间夹一根无成交，连续性就断了。

    ⚠️ 复刻假设：研报没写无成交 bar 算不算数。取"打断"而非"透明跳过" ——
    透明跳过是在断言"这段时间价格没动过"，比数据支持的更强。
    """
    falling = [
        Bar(open=4000.0, high=4100.0 - i * 5, low=3990.0, close=4000.0)
        for i in range(6)
    ]
    intact_bars = _rising(3) + falling
    atr = [None, None, None] + [1e9] * 6  # ATR 极大 ⇒ 吊灯永不触发，只留反向族

    intact = simulate_session(
        bars=intact_bars,
        atr_at=atr,
        fill_price=lambda i: intact_bars[i].close,
        next_session_open=4100.0,
    )

    broken_bars = list(intact_bars)
    broken_bars[5] = None
    broken = simulate_session(
        bars=broken_bars,
        atr_at=atr,
        fill_price=lambda i: broken_bars[i].close,
        next_session_open=4100.0,
    )

    assert [f.bar_index for f in intact.fills if f.kind is FillKind.SCALE_DOWN] != []
    assert [f.bar_index for f in broken.fills if f.kind is FillKind.SCALE_DOWN] == []


def test_a_short_day_close_uses_the_last_traded_bar():
    """空头当日收盘平仓；若最后一根无成交，用最近一根**有成交**的收盘价。"""
    bars = [
        Bar(open=4000.0 - i, high=4005.0 - i, low=3995.0 - i, close=3998.0 - i)
        for i in range(5)
    ]
    bars.append(None)

    result = simulate_session(
        bars=bars,
        atr_at=[None, None, None] + [1e9] * 3,
        fill_price=lambda i: bars[i].close,
        next_session_open=None,
    )

    exits = [f for f in result.fills if f.kind is FillKind.DAY_CLOSE_EXIT]
    assert result.direction is Direction.SHORT
    assert len(exits) == 1
    assert exits[0].price == 3994.0  # bars[4].close，不是那根无成交的
    assert exits[0].bar_index == 4


def test_a_long_that_meets_only_no_trade_bars_still_carries_overnight():
    bars = _rising(6)
    bars[3] = bars[4] = bars[5] = None

    result = simulate_session(
        bars=bars,
        atr_at=[None, None, None] + [1e9] * 3,
        fill_price=lambda i: bars[i].close,
        next_session_open=4100.0,
    )

    assert result.carried_overnight == pytest.approx(1.0)


def test_a_short_whose_every_post_entry_bar_is_absent_is_a_hard_failure():
    """空头必须当日平掉，可连一根有成交的 bar 都没有 ⇒ 没有成交价，硬失败。

    静默留仓过夜等于把研报的隔夜规则改掉，那是比报错严重得多的错。
    """
    bars = [
        Bar(open=4000.0 - i, high=4005.0 - i, low=3995.0 - i, close=3998.0 - i)
        for i in range(3)
    ]
    bars += [None, None]

    with pytest.raises(ValueError, match="no traded bar"):
        simulate_session(
            bars=bars,
            atr_at=[None, None, None, 1e9, 1e9],
            fill_price=lambda i: bars[i].close,
            next_session_open=None,
        )
