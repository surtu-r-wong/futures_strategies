"""主力合约选取、对账与分钟 candidate 构造 —— 计划 Task 7（离线核心）。

Task 7 分两半：**选哪个合约、取哪段时间**（本文件，纯函数，合成数据可完整验证）
与**真去库里取**（复用 `common/minute/pg_source.PublicMinuteSource`，其查询纪律、
EXPLAIN 闸与流式读取都已有自己的测试家族）。

⚠️ 2026-08-27 逐月实测的取数边界（`max(trade_date)` 会骗人，必须逐月看）：

- `public.futures_daily` 的 IF/IC/IH/IM **连续覆盖到 2026-04**，2026-05/06/07
  **一行都没有**，2026-08 只有孤零零一天 4 行。所以它的可用上界是 **2026-04-29**。
- `continuous_contract_ohlc` 整体停在 2026-04-29 ⇒ 只能当**对账参照**，不能当数据源。
- `futures_minute` 跑到 **2026-08-11**，`futures_contract_info` 从 2025-12-22 起连续
  ⇒ 尾部那三个半月靠"名单取 contract_info、量取 minute"这条通路补。
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from common.minute.pg_source import minute_contract_identity
from index_open_momentum.pg_source import (
    CFFEX_MULTIPLIERS,
    DOMINANT_SELECTION_LAG,
    metadata_multipliers_for,
    DominantChoice,
    build_index_candidates,
    choose_dominant,
    daily_stats_from_minutes,
    is_concrete_index_contract,
    reconcile_dominant,
)
from index_open_momentum.sessions import load_index_session_rules

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _daily(rows):
    """rows = (trade_date, symbol, oi, volume)"""
    return pd.DataFrame(
        [{"trade_date": d, "symbol": s, "oi": oi, "volume": v} for d, s, oi, v in rows]
    )


# --------------------------------------------------------------------------
# 1. 主力合约选取
# --------------------------------------------------------------------------


def test_the_contract_with_the_most_open_interest_wins():
    daily = _daily(
        [
            (date(2016, 6, 1), "IF1606.CFE", 50_000, 90_000),
            (date(2016, 6, 1), "IF1609.CFE", 10_000, 5_000),
            (date(2016, 6, 2), "IF1606.CFE", 50_000, 90_000),
            (date(2016, 6, 2), "IF1609.CFE", 10_000, 5_000),
        ]
    )

    choices = choose_dominant(daily, products=("IF",))

    assert [c.contract for c in choices] == ["IF1606.CFE"]
    assert choices[0].trade_date == date(2016, 6, 2)


def test_selection_uses_the_previous_session_so_it_is_causal():
    """⚠️ 复刻假设：持仓量当日收盘才知道，拿它选**当日**要交易的合约是回看。

    所以选取滞后一个交易日：6-02 交易哪张合约，由 6-01 的持仓量决定。
    """
    daily = _daily(
        [
            (date(2016, 6, 1), "IF1606.CFE", 50_000, 90_000),
            (date(2016, 6, 1), "IF1609.CFE", 10_000, 5_000),
            # 6-02 换月了，但 6-02 当天交易的仍应是 6-01 的主力
            (date(2016, 6, 2), "IF1606.CFE", 8_000, 4_000),
            (date(2016, 6, 2), "IF1609.CFE", 60_000, 95_000),
        ]
    )

    choices = choose_dominant(daily, products=("IF",))

    assert [(c.trade_date, c.contract) for c in choices] == [
        (date(2016, 6, 2), "IF1606.CFE")
    ]
    assert choices[0].selected_from == date(2016, 6, 1)


def test_the_lag_is_one_session_by_default():
    assert DOMINANT_SELECTION_LAG == 1


def test_volume_breaks_an_open_interest_tie():
    daily = _daily(
        [
            (date(2016, 6, 1), "IF1606.CFE", 50_000, 90_000),
            (date(2016, 6, 1), "IF1609.CFE", 50_000, 95_000),
            (date(2016, 6, 2), "IF1606.CFE", 1, 1),
            (date(2016, 6, 2), "IF1609.CFE", 1, 1),
        ]
    )

    choices = choose_dominant(daily, products=("IF",))

    assert choices[0].contract == "IF1609.CFE"


def test_open_interest_outranks_volume_when_they_disagree():
    """判据的**次序**要能被区分：持仓量高、成交量低的那张才是主力。

    ⚠️ 这条是变异验证逼出来的 —— 原来的两个用例里持仓量高的那张成交量也高，
    "先排 oi"和"先排 volume"给出同一答案，次序根本没被测到。
    """
    daily = _daily(
        [
            # IF1606 持仓量高但成交量低；IF1609 反过来
            (date(2016, 6, 1), "IF1606.CFE", 50_000, 10_000),
            (date(2016, 6, 1), "IF1609.CFE", 20_000, 95_000),
            (date(2016, 6, 2), "IF1606.CFE", 1, 1),
            (date(2016, 6, 2), "IF1609.CFE", 1, 2),
        ]
    )

    choices = choose_dominant(daily, products=("IF",))

    assert choices[0].contract == "IF1606.CFE"
    assert (choices[0].oi, choices[0].volume) == (50_000, 10_000)


def test_a_tie_on_both_open_interest_and_volume_is_a_hard_failure():
    """两条判据都平手就没有"主力"可言，硬失败而不是按字典序取一个。"""
    daily = _daily(
        [
            (date(2016, 6, 1), "IF1606.CFE", 50_000, 90_000),
            (date(2016, 6, 1), "IF1609.CFE", 50_000, 90_000),
            (date(2016, 6, 2), "IF1606.CFE", 1, 1),
            (date(2016, 6, 2), "IF1609.CFE", 1, 1),
        ]
    )

    with pytest.raises(ValueError, match="dominant_tie"):
        choose_dominant(daily, products=("IF",))


def test_products_are_kept_apart():
    daily = _daily(
        [
            (date(2016, 6, 1), "IF1606.CFE", 50_000, 90_000),
            (date(2016, 6, 1), "IH1606.CFE", 20_000, 30_000),
            (date(2016, 6, 2), "IF1606.CFE", 1, 1),
            (date(2016, 6, 2), "IH1606.CFE", 1, 1),
        ]
    )

    choices = choose_dominant(daily, products=("IF", "IH"))

    assert sorted(c.contract for c in choices) == ["IF1606.CFE", "IH1606.CFE"]


def test_the_first_session_has_no_predecessor_and_is_dropped():
    """第一根交易日没有前一日可用 ⇒ 它不出现在结果里，而不是拿自己选自己。"""
    daily = _daily(
        [
            (date(2016, 6, 1), "IF1606.CFE", 50_000, 90_000),
            (date(2016, 6, 2), "IF1606.CFE", 50_000, 90_000),
        ]
    )

    choices = choose_dominant(daily, products=("IF",))

    assert [c.trade_date for c in choices] == [date(2016, 6, 2)]


def test_a_product_absent_from_the_frame_is_a_hard_failure():
    daily = _daily([(date(2016, 6, 1), "IF1606.CFE", 1, 1)])

    with pytest.raises(ValueError, match="dominant_missing_product"):
        choose_dominant(daily, products=("IF", "IM"))


# --------------------------------------------------------------------------
# 2. 与连续合约的对账
# --------------------------------------------------------------------------


def _choice(trade_date, contract, product="IF"):
    return DominantChoice(
        trade_date=trade_date,
        product=product,
        contract=contract,
        oi=1,
        volume=1,
        selected_from=date(2016, 5, 31),
    )


def test_agreement_with_the_continuous_contract_is_recorded():
    choices = (_choice(date(2016, 6, 1), "IF1606.CFE"),)
    reference = {(date(2016, 6, 1), "IF"): "IF1606.CFE"}

    reconciled = reconcile_dominant(choices, reference=reference)

    assert reconciled[0].agrees is True
    assert reconciled[0].reference_contract == "IF1606.CFE"


def test_a_disagreement_is_reported_and_the_own_choice_is_kept():
    """研报口径下我们自己选的才是可跑到 08-11 的那个。分歧要报出来，不许静默取一边。"""
    choices = (_choice(date(2016, 6, 1), "IF1606.CFE"),)
    reference = {(date(2016, 6, 1), "IF"): "IF1609.CFE"}

    reconciled = reconcile_dominant(choices, reference=reference)

    assert reconciled[0].agrees is False
    assert reconciled[0].contract == "IF1606.CFE"
    assert reconciled[0].reference_contract == "IF1609.CFE"


def test_outside_the_reference_window_agreement_is_unknown_not_false():
    """连续合约只到 2026-04-29。其后没有参照 ⇒ `agrees is None`，不是"不一致"。"""
    choices = (_choice(date(2026, 6, 1), "IF2606.CFE"),)

    reconciled = reconcile_dominant(choices, reference={})

    assert reconciled[0].agrees is None
    assert reconciled[0].reference_contract is None


# --------------------------------------------------------------------------
# 3. 分钟 candidate 构造
# --------------------------------------------------------------------------


def test_a_candidate_covers_exactly_the_day_session():
    rules = load_index_session_rules()
    choices = (_choice(date(2016, 6, 1), "IF1606.CFE"),)

    candidates = build_index_candidates(choices, rules=rules)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.window_start == datetime(2016, 6, 1, 9, 30, tzinfo=SHANGHAI)
    # 末根 bar 开在 14:59，窗口右端取开区间 15:00
    assert candidate.window_end == datetime(2016, 6, 1, 15, 0, tzinfo=SHANGHAI)


def test_an_early_era_candidate_reaches_the_official_1515_close():
    """窗口按**官方**时段取，不迁就本库缺的那 15 分钟 —— 补上数据后不必改代码。"""
    rules = load_index_session_rules()
    choices = (_choice(date(2011, 6, 1), "IF1106.CFE"),)

    candidates = build_index_candidates(choices, rules=rules)

    assert candidates[0].window_start == datetime(2011, 6, 1, 9, 15, tzinfo=SHANGHAI)
    assert candidates[0].window_end == datetime(2011, 6, 1, 15, 15, tzinfo=SHANGHAI)


def test_the_candidate_carries_the_bare_minute_symbol():
    rules = load_index_session_rules()
    choices = (_choice(date(2016, 6, 1), "IF1606.CFE"),)

    candidates = build_index_candidates(choices, rules=rules)

    assert candidates[0].minute_symbol == "IF1606"
    assert candidates[0].exchange == "CFFEX"
    assert candidates[0].daily_contract == "IF1606.CFE"


def test_cffex_is_a_known_venue_in_the_shared_mapping():
    """`.CFE` 原本不在共享层的交易所映射里 —— 本 Task 把它补上了。"""
    product, symbol, exchange = minute_contract_identity("IF1606.CFE", date(2016, 6, 1))

    assert (product, symbol, exchange) == ("IF", "IF1606", "CFFEX")


def test_the_candidate_records_how_it_was_selected():
    rules = load_index_session_rules()
    choices = (_choice(date(2016, 6, 1), "IF1606.CFE"),)

    candidates = build_index_candidates(choices, rules=rules)

    assert candidates[0].selection_source == "daily_open_interest"
    assert candidates[0].causal_in_pool_date == date(2016, 5, 31)


def test_a_trade_date_with_no_session_rule_is_a_hard_failure():
    """IM 上市前没有时段规则 ⇒ 构造不出窗口，硬失败。"""
    rules = load_index_session_rules()
    choices = (_choice(date(2016, 6, 1), "IM1606.CFE", product="IM"),)

    with pytest.raises(Exception, match="session_rule_cardinality"):
        build_index_candidates(choices, rules=rules)


# --------------------------------------------------------------------------
# 4. 尾部数据源：`futures_daily` 之外的三个半月
# --------------------------------------------------------------------------
#
# ⚠️ 2026-08-27 逐月实测更正了一个先前的误读：`futures_daily` 的 IF 在
# **2026-05/06/07 一行都没有**，2026-08 只有孤零零一天 4 行 —— 早先看到的
# `max(trade_date) = 2026-08-26` 是那一天造成的，不是连续覆盖。
# 而 `futures_minute` 跑到 2026-08-11。所以尾部要另一条通路：
# 合约名单取 `futures_contract_info`（2025-12-22 起连续），持仓量与成交量
# 取 `futures_minute` 自身。


def test_a_concrete_month_contract_is_recognised():
    assert is_concrete_index_contract("IF2606")
    assert is_concrete_index_contract("IM2703")


@pytest.mark.parametrize(
    "symbol",
    ["IF00", "IF001", "IF01", "IF12", "IFL0", "IFL00", "IFmain", "IFJQ00"],
)
def test_wind_synthetic_codes_are_rejected(symbol):
    """`futures_contract_info` 里混着 Wind 的连续/主力合成码，全都不是可交易合约。

    ⚠️ `IF01`~`IF12` 这一族最像真合约（四位以内的数字），但真合约是**四位** ——
    `IF2606`。判据必须钉在位数上，不能只看"有没有数字"。
    """
    assert not is_concrete_index_contract(symbol)


def test_the_listed_set_is_what_cffex_actually_lists():
    """实测 2026-06-01 的 IF 真合约恰好是 当月/次月/两个季月 四张。"""
    frame = pd.DataFrame(
        {
            "合约代码": [
                "IF00",
                "IF001",
                "IF01",
                "IFL0",
                "IFmain",
                "IF2606",
                "IF2607",
                "IF2609",
                "IF2612",
            ]
        }
    )

    concrete = [s for s in frame["合约代码"] if is_concrete_index_contract(s)]

    assert concrete == ["IF2606", "IF2607", "IF2609", "IF2612"]


def test_minute_derived_stats_take_the_last_bar_open_interest():
    """持仓量是**时点**量，日内取最后一根；成交量是**流量**，日内求和。

    弄反了会让持仓量变成一个没有意义的累加数，而且它恰好也是单调的，
    "看起来对"——所以必须钉住。
    """
    bars = pd.DataFrame(
        [
            {
                "trade_date": date(2026, 6, 1),
                "symbol": "IF2606",
                "bar_time": datetime(2026, 6, 1, 9, 30, tzinfo=SHANGHAI),
                "volume": 10,
                "open_interest": 50_000,
            },
            {
                "trade_date": date(2026, 6, 1),
                "symbol": "IF2606",
                "bar_time": datetime(2026, 6, 1, 14, 59, tzinfo=SHANGHAI),
                "volume": 30,
                "open_interest": 47_000,
            },
        ]
    )

    stats = daily_stats_from_minutes(bars)

    assert len(stats) == 1
    assert int(stats.iloc[0]["oi"]) == 47_000
    assert int(stats.iloc[0]["volume"]) == 40


def test_minute_derived_stats_feed_choose_dominant_unchanged():
    """两条通路产出同一个形状，所以选主力的逻辑只有一份。"""
    bars = pd.DataFrame(
        [
            {
                "trade_date": d,
                "symbol": s,
                "bar_time": datetime(d.year, d.month, d.day, 14, 59, tzinfo=SHANGHAI),
                "volume": v,
                "open_interest": oi,
            }
            for d, s, oi, v in [
                (date(2026, 6, 1), "IF2606", 50_000, 90_000),
                (date(2026, 6, 1), "IF2609", 10_000, 5_000),
                (date(2026, 6, 2), "IF2606", 1, 1),
                (date(2026, 6, 2), "IF2609", 1, 1),
            ]
        ]
    )

    choices = choose_dominant(daily_stats_from_minutes(bars), products=("IF",))

    assert [(c.trade_date, c.contract) for c in choices] == [
        (date(2026, 6, 2), "IF2606")
    ]


def test_the_exchange_multipliers_match_what_contract_info_reports():
    """交易所事实：IF/IH = 300，IC/IM = 200。

    2026-08-27 与 `public.futures_contract_info` 核对一致（该表 2025-12-22 起可用）。
    CLI 拿它当元数据兜底：换月后新主力头两天样本不够反推，没有兜底会硬失败 ——
    实测 2016-03-18 的 `IF1604` 就是这种情形。兜底不放松校验：样本一够仍要过价域闸。
    """
    assert CFFEX_MULTIPLIERS == {"IF": 300, "IH": 300, "IC": 200, "IM": 200}


def test_metadata_multipliers_are_keyed_by_minute_symbol():
    choices = (_choice(date(2016, 6, 1), "IF1606.CFE"),)
    candidates = build_index_candidates(choices, rules=load_index_session_rules())

    mapping = metadata_multipliers_for(candidates)

    assert mapping == {"IF1606": 300}
