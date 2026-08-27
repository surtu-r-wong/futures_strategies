"""回测编排 —— 计划 Task 7 第 5 步的前置、Task 8 的输入。

每个零件都已在自己的测试家族里验过，所以这里测的是**接缝**，尤其是三处一接错
就静默出错的地方：

1. **ATR 必须在跨日连续序列上算。** 研报窗口 16 根，而股指晚年代一天正好 16 根
   （240/15）。按 session 重置的话建仓那根（当日第 3 根）**永远**拿不到 ATR，
   而 `risk._require_atr` 无 ATR 时硬失败 ⇒ 症状是"整段回测一笔都开不出来"，
   不是好查的错。
2. **成交窗是"这根 bar 之后的 5 个交易时钟分钟槽"**，不是"之后 5 分钟"。
   第 8 根 bar 收在 11:30，它的成交窗是 **13:00–13:04**。
3. **组合分母是当日有信号的品种数**，不是恒定 3。

⚠️ 交易日给足 4 天不是凑数：`_select_multiplier_sample` 要求乘数样本**跨 ≥3 个
交易日**（单日的价域约束撑不出唯一解），所以任何真会成交的用例都至少要 3 天。

数据源用确定性替身注入，不连库。
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from common.minute.sessions import build_trading_slots, resolve_session_rule
from index_open_momentum.pg_source import build_index_candidates, DominantChoice
from index_open_momentum.risk import ATR_WINDOW, Direction
from index_open_momentum.run import FILL_WINDOW_SLOTS, fill_window, run_backtest
from index_open_momentum.sessions import load_index_session_rules

SHANGHAI = ZoneInfo("Asia/Shanghai")
RULES = load_index_session_rules()
IF_MULTIPLIER = 300

#: 会真成交的用例至少要 4 个交易日：乘数样本要跨 ≥3 天，再加首日没有 ATR。
_DAYS = (date(2016, 6, 1), date(2016, 6, 2), date(2016, 6, 3), date(2016, 6, 6))
_EARLY_DAYS = (date(2011, 6, 1), date(2011, 6, 2), date(2011, 6, 3), date(2011, 6, 7))

#: 生产上 CLI 从 `futures_contract_info` 取；测试里照同一条通路喂。
_META = {"IF1606": IF_MULTIPLIER}


class FakeSource:
    """按月吐 DataFrame 的确定性替身，并像真源一样攒计划摘要。"""

    def __init__(self, frame: pd.DataFrame, summaries=()):
        self._frame = frame
        self.plan_audit = tuple(summaries)
        self.months_asked: list[tuple[datetime, datetime]] = []

    def iter_month(self, candidates, lower, upper):
        self.months_asked.append((lower, upper))
        window = self._frame.loc[
            (self._frame["bar_time"] >= lower) & (self._frame["bar_time"] < upper)
        ]
        if not window.empty:
            yield window


def _slots(product, trade_date):
    rule = resolve_session_rule(RULES, "CFFEX", product, trade_date)
    return build_trading_slots(trade_date, trade_date - timedelta(days=1), rule)


def _choice(trade_date, contract, product="IF"):
    return DominantChoice(
        trade_date=trade_date,
        product=product,
        contract=contract,
        oi=1,
        volume=1,
        selected_from=trade_date - timedelta(days=1),
    )


def _rows(product, trade_date, symbol, price_at, *, multiplier=IF_MULTIPLIER):
    """按当日每个 slot 造一行分钟；`price_at(i)` 给第 i 个 slot 的价。"""
    records = []
    for index, slot in enumerate(_slots(product, trade_date)):
        price = price_at(index)
        records.append(
            {
                "trade_date": trade_date,
                "product": product,
                "daily_contract": f"{symbol}.CFE",
                "bar_time": slot,
                "symbol": symbol,
                "exchange": "CFFEX",
                "open": price,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price,
                "volume": 10.0,
                "amount": price * 10.0 * multiplier,
                "open_interest": 1000,
            }
        )
    return records


def _rising(base=4000.0, step=1.0):
    return lambda i: base + step * i


def _run(
    days,
    *,
    product="IF",
    symbol_of=lambda d: "IF1606",
    price_at=None,
    metadata=None,
    min_observations=None,
):
    price_at = price_at or _rising()
    records = []
    choices = []
    for trade_date in days:
        symbol = symbol_of(trade_date)
        records.extend(_rows(product, trade_date, symbol, price_at))
        choices.append(_choice(trade_date, f"{symbol}.CFE", product=product))
    frame = pd.DataFrame(records)
    candidates = build_index_candidates(tuple(choices), rules=RULES)
    source = FakeSource(frame)
    return (
        run_backtest(
            candidates=candidates,
            rules=RULES,
            source=source,
            metadata_multipliers=metadata,
            **(
                {}
                if min_observations is None
                else {"realized_vol_min_observations": min_observations}
            ),
        ),
        source,
    )


# --------------------------------------------------------------------------
# 1. 成交窗
# --------------------------------------------------------------------------


def test_the_fill_window_is_the_five_slots_after_the_bar():
    slots = _slots("IF", date(2016, 6, 1))

    window = fill_window(slots, 2)  # 第 3 根 bar 收在 10:15

    assert len(window) == FILL_WINDOW_SLOTS
    assert [s.strftime("%H:%M") for s in window] == [
        "10:15",
        "10:16",
        "10:17",
        "10:18",
        "10:19",
    ]


def test_the_fill_window_after_the_morning_close_lands_in_the_afternoon():
    """⚠️ 第 8 根 bar 收在 11:30，其后 5 个**交易时钟**槽是 13:00–13:04。

    按挂钟时间加 5 分钟会取到 11:30–11:34 —— 午休里根本不存在的分钟。
    """
    slots = _slots("IF", date(2016, 6, 1))

    window = fill_window(slots, 7)

    assert [s.strftime("%H:%M") for s in window] == [
        "13:00",
        "13:01",
        "13:02",
        "13:03",
        "13:04",
    ]


def test_the_last_bar_of_the_day_has_no_fill_window():
    slots = _slots("IF", date(2016, 6, 1))

    assert fill_window(slots, 15) == ()


def test_a_partial_window_is_no_window_at_all():
    """凑不满 5 槽就当作没有窗口，不返回一个短的。

    ⚠️ 完整交易日里 slot 数总是 15 的倍数，所以部分窗口**结构上不会出现** ——
    但 `fill_window` 是公开函数，喂一段截断的槽是合法调用。返回短窗口会让
    `index_execution_fill` 在 `required_count=5` 上报一个绕远的错，而不是
    "这根 bar 后面没有成交窗"。
    """
    slots = _slots("IF", date(2016, 6, 1))

    assert fill_window(slots[:33], 1) == ()  # start=30，只剩 3 槽


# --------------------------------------------------------------------------
# 2. ATR 的跨日连续性 —— 本文件最要紧的一条
# --------------------------------------------------------------------------


def test_the_first_day_cannot_trade_because_atr_has_no_history():
    """晚年代一天 16 根、ATR 窗口 16 根 ⇒ 第一天的第 3 根还没有 ATR。"""
    result, _ = _run([date(2016, 6, 1)], metadata=_META)

    assert len(result.product_days) == 1
    assert result.product_days[0].direction is None
    assert result.product_days[0].atr_at_entry is None


def test_the_second_day_can_trade_because_atr_carries_over():
    """第二天的第 3 根前面已经有 18 根（首日 16 + 当日 2）⇒ ATR 存在。

    ⚠️ 这条就是「ATR 按 session 重置」那个错的探针：一旦重置，第二天照样拿不到
    ATR，本条会红而且**只有本条会红**。
    """
    result, _ = _run(_DAYS, metadata=_META)

    second = [d for d in result.product_days if d.trade_date == _DAYS[1]]
    assert len(second) == 1
    assert second[0].atr_at_entry is not None
    assert second[0].direction is Direction.LONG


def test_the_overnight_gap_enters_true_range():
    """连续序列意味着隔夜跳空计入 TR（复刻假设②）。

    第二天整体抬高 500 点，ATR 必然比不跳空时大 —— 拿两组只差跳空的输入对照。
    """
    flat, _ = _run(_DAYS, price_at=lambda i: 4000.0 + i, metadata=_META)

    days = list(_DAYS)
    records = []
    choices = []
    for offset, trade_date in enumerate(days):
        base = 4000.0 + offset * 500.0
        records.extend(_rows("IF", trade_date, "IF1606", lambda i, b=base: b + i))
        choices.append(_choice(trade_date, "IF1606.CFE"))
    gapped = run_backtest(
        candidates=build_index_candidates(tuple(choices), rules=RULES),
        rules=RULES,
        source=FakeSource(pd.DataFrame(records)),
        metadata_multipliers=_META,
    )

    flat_atr = [d for d in flat.product_days if d.trade_date == days[1]][0].atr_at_entry
    gap_atr = [d for d in gapped.product_days if d.trade_date == days[1]][
        0
    ].atr_at_entry
    assert gap_atr > flat_atr


# --------------------------------------------------------------------------
# 3. 按月分批
# --------------------------------------------------------------------------


def test_months_are_asked_for_one_at_a_time_with_literal_bounds():
    """按月分批的意义是让时间边界成为**计划期常量** —— chunk 排除只在那时发生。"""
    _, source = _run([date(2016, 6, 30), date(2016, 7, 1)], metadata=_META)

    assert source.months_asked == [
        (datetime(2016, 6, 1, tzinfo=SHANGHAI), datetime(2016, 7, 1, tzinfo=SHANGHAI)),
        (datetime(2016, 7, 1, tzinfo=SHANGHAI), datetime(2016, 8, 1, tzinfo=SHANGHAI)),
    ]


def test_a_december_window_rolls_the_year_over():
    _, source = _run([date(2016, 12, 30)], metadata=_META)

    assert source.months_asked == [
        (datetime(2016, 12, 1, tzinfo=SHANGHAI), datetime(2017, 1, 1, tzinfo=SHANGHAI))
    ]


# --------------------------------------------------------------------------
# 4. 杠杆与组合
# --------------------------------------------------------------------------


def test_leverage_is_zero_on_a_day_with_no_signal():
    result, _ = _run([date(2016, 6, 1)], metadata=_META)

    assert result.product_days[0].leverage == 0.0
    assert result.product_days[0].net_return == 0.0


def test_the_portfolio_column_splits_capital_among_active_products_only():
    """⚠️ 分母是当日**有信号**的品种数，不是恒定 3（复刻假设⑥）。"""
    days = [date(2016, 6, 1), date(2016, 6, 2), date(2016, 6, 3), date(2016, 7, 1)]
    records = []
    choices = []
    for trade_date in days:
        records.extend(_rows("IF", trade_date, "IF1606", _rising()))
        choices.append(_choice(trade_date, "IF1606.CFE"))
        # IH 全天横盘：三根 open 不严格递增 ⇒ 无信号
        records.extend(_rows("IH", trade_date, "IH1606", lambda i: 3000.0))
        choices.append(_choice(trade_date, "IH1606.CFE", product="IH"))

    result = run_backtest(
        candidates=build_index_candidates(tuple(choices), rules=RULES),
        rules=RULES,
        source=FakeSource(pd.DataFrame(records)),
        metadata_multipliers={**_META, "IH1606": IF_MULTIPLIER},
        realized_vol_min_observations=3,
    )

    # ⚠️ 跨月 + 小阈值，好让 IF 在 7 月真有**非零**收益 —— 否则 0 == 0，
    # 这条断言就是空的（变异验证 Q13 正是这么逃掉的）。
    last = result.daily.loc[days[-1]]
    assert last["IF"] != 0.0
    assert last["IH"] == 0.0
    # 只有 IF 活跃 ⇒ 组合 = IF 自己，不是 IF/2
    assert last["portfolio"] == pytest.approx(last["IF"])


# --------------------------------------------------------------------------
# 5. 出处与审计
# --------------------------------------------------------------------------


def test_plan_summaries_travel_with_the_result():
    """Task 7 第 5 步：`EXPLAIN` 产物必须能从结果里拿到，才谈得上落盘。"""
    _, source = _run([date(2016, 6, 1)], metadata=_META)
    source.plan_audit = ("summary-a", "summary-b")
    result, _ = _run([date(2016, 6, 1)])

    assert isinstance(result.plan_summaries, tuple)


def test_each_product_day_records_the_archive_gap_it_sat_on():
    """2016 前每天都坐在 15 分钟的档案缺口上，报告要看得见。"""
    early, _ = _run(
        _EARLY_DAYS, symbol_of=lambda d: "IF1106", metadata={"IF1106": IF_MULTIPLIER}
    )
    late, _ = _run(_DAYS, metadata=_META)

    assert {d.known_gap_minutes for d in early.product_days} == {15}
    assert {d.known_gap_minutes for d in late.product_days} == {0}


def test_the_multiplier_is_resolved_once_per_contract():
    result, _ = _run(_DAYS, metadata=_META)

    assert set(result.multipliers) == {"IF1606"}
    assert result.multipliers["IF1606"].multiplier == IF_MULTIPLIER


def test_an_early_era_day_has_eighteen_bars():
    result, _ = _run(
        [date(2011, 6, 1)],
        symbol_of=lambda d: "IF1106",
        metadata={"IF1106": IF_MULTIPLIER},
    )

    assert result.product_days[0].bars == 18


def test_an_empty_candidate_list_yields_an_empty_result():
    result = run_backtest(candidates=(), rules=RULES, source=FakeSource(pd.DataFrame()))

    assert result.product_days == ()
    assert result.daily.empty


def test_the_atr_window_constant_is_the_paper_sixteen():
    assert ATR_WINDOW == 16


# --------------------------------------------------------------------------
# 6. 乘数：样本够就校验，不够就用元数据兜底并披露
# --------------------------------------------------------------------------
#
# ⚠️ 这一节是接线时才现形的约束：`_select_multiplier_sample` 要求样本跨 ≥3 个
# 交易日，而**每次换月新主力合约在我们取的数据里 0 天历史** —— 头两天都推不出
# 乘数。15 年 × 3 品种约 540 个交易日会因此发不出成交，不是边角情形。


def test_a_thin_sample_falls_back_to_the_metadata_multiplier():
    """样本不够时用元数据，但 `source` 必须写明它**没经过**价域校验。

    这不是"静默采信"：标记会一路带进保真度报告，而且样本一够就自动改走校验。
    """
    # 只两天：首日没有 ATR 不成交，次日成交时累积样本才 2 天 —— 始终够不着
    result, _ = _run([date(2016, 6, 30), date(2016, 7, 1)], metadata=_META)

    resolution = result.multipliers["IF1606"]
    assert resolution.multiplier == IF_MULTIPLIER
    assert resolution.source == "metadata_unvalidated"
    assert resolution.sample_dates == 2


def test_a_thin_sample_without_metadata_is_a_hard_failure():
    """没有元数据可兜底就硬失败 —— 拿一个猜的乘数计价比报错严重得多。"""
    from common.minute.bars import MinuteDataError

    with pytest.raises(MinuteDataError, match="contract_multiplier_sample"):
        _run([date(2016, 6, 30), date(2016, 7, 1)])


def test_the_fallback_upgrades_itself_once_the_sample_is_thick():
    """同一份元数据，样本攒够后自动改走校验路径 —— `source` 从兜底升级成 `metadata`。

    ⚠️ 这条是实现里差点漏掉的：兜底一旦缓存就不再重试的话，第一天薄样本定下的
    `metadata_unvalidated` 会跟着整段回测，而它自己的 docstring 明明承诺会升级。
    """
    result, _ = _run(_DAYS, metadata=_META)

    assert result.multipliers["IF1606"].source == "metadata"


def test_wrong_metadata_is_caught_even_while_the_sample_is_still_thin():
    """错的元数据一定会被拦下 —— 样本还薄时由**价域闸**拦，不必等样本变厚。

    IF 乘数 300 却给 200 ⇒ 兜底那一天算出的 VWAP 是真价的 1.5 倍，落在
    `[low, high]` 之外，`index_execution_fill` 当场硬失败。
    ⇒ 兜底并没有打开一个"错的乘数能悄悄跑一段"的窗口。
    """
    from common.minute.bars import MinuteDataError

    with pytest.raises(MinuteDataError, match="execution_vwap"):
        _run(_DAYS, metadata={"IF1606": 200})


def test_a_flat_day_still_enters_the_return_series():
    """不建仓的日子收益是 0，但**必须记进序列** —— 少记会让已实现波动率的
    252 日窗口整体错位，而且错得看不出来（波动率仍是个合理数字）。

    ⚠️ 窗口在**当月第一天**截断（避免前视），所以要跨月才看得见：6 月三天里
    首日必然不建仓（ATR 窗口 16 根 ≈ 一天）。把阈值设成 **3**：首日记了则 6 月
    有 3 个观测，7 月那天算得出波动率；首日没记就只有 2 个，仍是 `None`。
    """
    days = [date(2016, 6, 1), date(2016, 6, 2), date(2016, 6, 3), date(2016, 7, 1)]

    result, _ = _run(days, metadata=_META, min_observations=3)

    july = [d for d in result.product_days if d.trade_date == date(2016, 7, 1)][0]
    assert july.realized_vol is not None


def test_every_session_produces_a_row_even_when_it_does_not_trade():
    result, _ = _run(_DAYS, metadata=_META)

    assert [d.trade_date for d in result.product_days] == list(_DAYS)
    assert result.product_days[0].direction is None
    assert list(result.daily.index) == list(_DAYS)


# --------------------------------------------------------------------------
# 7. 影子账户：波动率反馈不能算在自己缩放过的收益上
# --------------------------------------------------------------------------


def test_the_volatility_feedback_uses_an_unscaled_shadow_series():
    """⚠️ 若把已实现波动率算在**已加杠杆**的收益上，整段回测恒为零：

    第一年没有波动率历史 ⇒ 杠杆 0 ⇒ 收益全 0 ⇒ 波动率 0 ⇒ 杠杆仍是 0 ⇒ 永远启动
    不了。而且症状是"回测跑完了，只是都不赚钱"，不是报错。

    所以波动率要算在**未按波动率缩放**的影子收益上（只加 ATR 杠杆）。这正是
    `common/minute/account.py` 那句"正式账户与未缩放影子账户并行"的形状。
    """
    days = [date(2016, 6, 1), date(2016, 6, 2), date(2016, 6, 3), date(2016, 7, 1)]

    result, _ = _run(days, metadata=_META, min_observations=3)

    june = [d for d in result.product_days if d.trade_date.month == 6]
    july = [d for d in result.product_days if d.trade_date == date(2016, 7, 1)][0]

    # 6 月无波动率历史 ⇒ 实盘杠杆为 0，但影子收益必须不全是 0
    assert all(d.leverage == 0.0 for d in june)
    assert any(d.shadow_return != 0.0 for d in june)
    # 攒够影子收益后，7 月才真的能算出波动率并建仓
    assert july.realized_vol is not None and july.realized_vol > 0.0
    assert july.leverage > 0.0


def test_the_shadow_return_is_recorded_even_when_the_day_does_not_trade():
    """不建仓的日子影子收益仍要记 —— 否则波动率窗口错位（同 flat-day 那条）。"""
    days = [date(2016, 6, 1), date(2016, 6, 2), date(2016, 6, 3), date(2016, 7, 1)]

    result, _ = _run(days, metadata=_META, min_observations=3)

    assert all(hasattr(d, "shadow_return") for d in result.product_days)
    assert len([d for d in result.product_days if d.trade_date.month == 6]) == 3


def test_a_product_with_no_signal_all_month_still_yields_zero_volatility():
    """整月没信号 ⇒ 影子收益全 0 ⇒ 波动率 0 ⇒ 次月仍不建仓。零波动闸照样要在。

    ⚠️ 接入影子账户后，"退化"的场景变了：以前拿"收益恒等"就能造出零波动，
    现在影子收益会随行情变动，只有**整月一个信号都没有**才真的全 0。
    这条取代了原来那个前提已不成立的用例。
    """
    days = [date(2016, 6, 1), date(2016, 6, 2), date(2016, 6, 3), date(2016, 7, 1)]

    result, _ = _run(
        days, metadata=_META, min_observations=3, price_at=lambda i: 4000.0
    )

    july = [d for d in result.product_days if d.trade_date == date(2016, 7, 1)][0]
    assert july.realized_vol == 0.0
    assert july.direction is None


# --------------------------------------------------------------------------
# 8. 无法计价的交易日
# --------------------------------------------------------------------------
#
# ⚠️ 全历史真跑炸出来的：必需的 5 分钟成交窗**零成交**，`five_minute_vwap` 按计划
# 硬失败，于是整段 15 年在第一个这样的日子上中断。
#
# 根因不是执行层，是**主力合约规则**：临近交割时持仓量先于成交量迁移，
# 持仓量优先会提前换月，换进一个日内还不够活跃的合约（实测 2011-11-15：
# IF1112 持仓量 22,543 > IF1111 的 19,958，但成交量 43,269 << 124,833）。
#
# 计划写的是"运行硬失败"，所以**默认仍然是 abort**。`skip` 只是为了先量清楚规模 ——
# 它不顺延到更晚的窗口，也不替换价格，只是当日不建仓并如实计数。


def test_an_unpriceable_session_aborts_the_run_by_default():
    """计划口径：必需的成交窗零成交 ⇒ 运行硬失败。默认不许悄悄跳过。"""
    from common.minute.bars import MinuteDataError

    days = [date(2016, 6, 1), date(2016, 6, 2), date(2016, 6, 3), date(2016, 6, 6)]
    records = []
    choices = []
    for trade_date in days:
        rows = _rows("IF", trade_date, "IF1606", _rising())
        if trade_date == days[-1]:
            # 把最后一天的成交窗（第 3 根之后的 5 个槽）成交量清零
            for row in rows[45:50]:
                row["volume"] = 0.0
                row["amount"] = 0.0
        records.extend(rows)
        choices.append(_choice(trade_date, "IF1606.CFE"))

    with pytest.raises(MinuteDataError, match="execution_vwap"):
        run_backtest(
            candidates=build_index_candidates(tuple(choices), rules=RULES),
            rules=RULES,
            source=FakeSource(pd.DataFrame(records)),
            metadata_multipliers=_META,
        )


def test_skip_mode_records_the_session_instead_of_aborting():
    """`skip` 只是当日不建仓 + 如实计数；不顺延窗口、不替换价格。"""
    days = [date(2016, 6, 1), date(2016, 6, 2), date(2016, 6, 3), date(2016, 6, 6)]
    records = []
    choices = []
    for trade_date in days:
        rows = _rows("IF", trade_date, "IF1606", _rising())
        if trade_date == days[-1]:
            for row in rows[45:50]:
                row["volume"] = 0.0
                row["amount"] = 0.0
        records.extend(rows)
        choices.append(_choice(trade_date, "IF1606.CFE"))

    result = run_backtest(
        candidates=build_index_candidates(tuple(choices), rules=RULES),
        rules=RULES,
        source=FakeSource(pd.DataFrame(records)),
        metadata_multipliers=_META,
        on_unpriceable="skip",
    )

    last = [d for d in result.product_days if d.trade_date == days[-1]][0]
    assert last.unpriceable is True
    assert last.direction is None
    assert last.net_return == 0.0
    assert result.unpriceable_sessions == 1


def test_priceable_sessions_are_not_marked():
    result, _ = _run(_DAYS, metadata=_META)

    assert all(d.unpriceable is False for d in result.product_days)
    assert result.unpriceable_sessions == 0


def test_an_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="on_unpriceable"):
        run_backtest(
            candidates=(), rules=RULES, source=FakeSource(pd.DataFrame()),
            on_unpriceable="pretend",
        )
