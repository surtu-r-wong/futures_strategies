"""CFFEX 时段版本与数据质量闸 —— 计划 Task 1。

三件事分开测：

1. `CFFEX_V1` 这个 ruleset 本身（两个年代的日盘段、无夜盘、时钟宽度）；
2. `config/index_minute_sessions.csv` 这份真资产（品种覆盖、生效区间、年代切换日）；
3. 两道闸 —— 已知缺口登记（2016 前 15:00–15:14 档案没有）与逐年覆盖度对账。

本文件的每个期望值都是手算字面量：
09:15=555 / 09:30=570 / 11:30=690 / 13:00=780 / 15:00=900 / 15:15=915（分钟自午夜起算）。
2016 前段长 (690-555)+(915-780)=135+135=**270**；2016 起 (690-570)+(900-780)=120+120=**240**。

⚠️ 实测（2026-08-27，`futures_minute` 抽样十个 contract-day）：2016 前库内只有 **255** 根，
比官方段长少的正好是 15:00–15:14 那 **15** 分钟；2016 起库内 240 根，与官方段长**相等**。
所以 ruleset 记官方口径、缺的那 15 分钟由已知缺口登记表承担 —— 这正是下面
`test_the_pre_2016_close_tail_is_registered_as_a_known_gap` 钉住的东西。
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from common.minute.sessions import (
    SESSION_RULESETS,
    SessionClockError,
    SessionSegment,
    build_trading_slots,
    fifteen_minute_buckets,
    resolve_session_rule,
)
from index_open_momentum.sessions import (
    CFFEX_V1,
    INDEX_SESSION_RULES_PATH,
    LISTING_DATES,
    coverage_gate,
    expected_absent_minutes,
    known_gaps_on,
    load_index_session_rules,
    require_bars_map_to_slots,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


# --------------------------------------------------------------------------
# 1. CFFEX_V1 本身
# --------------------------------------------------------------------------


def test_cffex_v1_is_registered_under_its_own_version():
    assert SESSION_RULESETS["cffex-v1"] is CFFEX_V1


def test_the_early_era_runs_0915_to_1130_and_1300_to_1515():
    assert CFFEX_V1.day_segments_for(date(2015, 12, 31)) == (
        SessionSegment(555, 690),
        SessionSegment(780, 915),
    )


def test_the_late_era_runs_0930_to_1130_and_1300_to_1500():
    assert CFFEX_V1.day_segments_for(date(2016, 1, 4)) == (
        SessionSegment(570, 690),
        SessionSegment(780, 900),
    )


def test_the_day_session_shortened_on_2016_01_01_exactly():
    """切换日钉在 2016-01-01：前一天仍是早年代，当天起是晚年代。

    实测佐证：`IF1601` 2015-12-31 共 255 根、首根 09:15；`IF1602` 2016-01-04
    共 240 根、首根 09:30。
    """
    assert CFFEX_V1.day_segments_for(date(2015, 12, 31))[0].start_minute == 555
    assert CFFEX_V1.day_segments_for(date(2016, 1, 1))[0].start_minute == 570


def test_cffex_has_no_night_session():
    assert CFFEX_V1.allows_night is False


def test_the_clock_is_wide_enough_for_the_1515_close():
    """15:15 = 第 915 分钟。时钟上界不到 915，2016 前的日盘根本构造不出来。"""
    assert CFFEX_V1.clock_end_minute >= 915
    assert CFFEX_V1.clock_start_minute <= 555


def test_capture_start_is_the_if_listing_day():
    assert CFFEX_V1.capture_start == date(2010, 4, 16)


# --------------------------------------------------------------------------
# 2. 真资产 config/index_minute_sessions.csv
# --------------------------------------------------------------------------


def test_the_asset_covers_all_four_index_products():
    rules = load_index_session_rules()

    assert {rule.product for rule in rules} == {"IF", "IC", "IH", "IM"}
    assert {rule.exchange for rule in rules} == {"CFFEX"}


def test_every_asset_rule_is_cffex_v1():
    assert {rule.version for rule in load_index_session_rules()} == {"cffex-v1"}


@pytest.mark.parametrize("product", ["IF", "IC", "IH", "IM"])
def test_each_product_starts_on_its_listing_day(product):
    rules = load_index_session_rules()
    earliest = min(rule.effective_start for rule in rules if rule.product == product)

    assert earliest == LISTING_DATES[product]


@pytest.mark.parametrize(
    "product,trade_date,expected",
    [
        # IF 挂牌 2010-04-16，横跨两个年代
        ("IF", date(2011, 6, 1), (SessionSegment(555, 690), SessionSegment(780, 915))),
        ("IF", date(2016, 6, 1), (SessionSegment(570, 690), SessionSegment(780, 900))),
        # IC/IH 挂牌 2015-04-16，也横跨
        ("IC", date(2015, 6, 1), (SessionSegment(555, 690), SessionSegment(780, 915))),
        ("IC", date(2016, 6, 1), (SessionSegment(570, 690), SessionSegment(780, 900))),
        # IM 挂牌 2022-07-22，只活在晚年代
        ("IM", date(2023, 8, 1), (SessionSegment(570, 690), SessionSegment(780, 900))),
    ],
)
def test_the_asset_resolves_to_the_segments_of_that_era(product, trade_date, expected):
    rule = resolve_session_rule(
        load_index_session_rules(), "CFFEX", product, trade_date
    )

    assert rule.segments == expected


def test_im_has_no_rule_before_it_listed():
    with pytest.raises(SessionClockError, match="session_rule_cardinality"):
        resolve_session_rule(
            load_index_session_rules(), "CFFEX", "IM", date(2016, 6, 1)
        )


def test_the_asset_declares_no_night_interval_anywhere():
    """无夜盘不是"CSV 里恰好没写"，是 ruleset 硬拒 —— 这里钉住资产本身也确实没写。"""
    body = INDEX_SESSION_RULES_PATH.read_text(encoding="utf-8")

    for line in body.splitlines()[1:]:
        assert line.split(",")[4:6] == ["none", "none"], line


# --------------------------------------------------------------------------
# 3. slot 与 15 分钟桶（把段长算术钉死）
# --------------------------------------------------------------------------


def _slots(product, trade_date, previous):
    rule = resolve_session_rule(
        load_index_session_rules(), "CFFEX", product, trade_date
    )
    return build_trading_slots(trade_date, previous, rule), rule


def test_an_early_era_day_has_270_official_minutes():
    slots, _ = _slots("IF", date(2011, 6, 1), date(2011, 5, 31))

    assert len(slots) == 270


def test_a_late_era_day_has_240_official_minutes():
    slots, _ = _slots("IF", date(2016, 6, 1), date(2016, 5, 31))

    assert len(slots) == 240


def test_the_lunch_break_is_not_bridged():
    """上午最后一分钟 11:29，下午第一分钟 13:00，中间不发 slot。"""
    slots, _ = _slots("IF", date(2016, 6, 1), date(2016, 5, 31))
    times = [slot.astimezone(SHANGHAI).strftime("%H:%M") for slot in slots]

    morning_end = times.index("11:29")
    assert times[morning_end + 1] == "13:00"


def test_the_first_three_buckets_of_the_early_era_open_at_0915():
    """开盘前 3 根 bar 必须按当期实际开盘时刻锚定，不得写死 09:30。"""
    slots, rule = _slots("IF", date(2011, 6, 1), date(2011, 5, 31))
    buckets = fifteen_minute_buckets(slots, rule)
    heads = [b[0].astimezone(SHANGHAI).strftime("%H:%M") for b in buckets[:3]]

    assert heads == ["09:15", "09:30", "09:45"]


def test_the_first_three_buckets_of_the_late_era_open_at_0930():
    slots, rule = _slots("IF", date(2016, 6, 1), date(2016, 5, 31))
    buckets = fifteen_minute_buckets(slots, rule)
    heads = [b[0].astimezone(SHANGHAI).strftime("%H:%M") for b in buckets[:3]]

    assert heads == ["09:30", "09:45", "10:00"]


# --------------------------------------------------------------------------
# 4. 已知缺口登记
# --------------------------------------------------------------------------


def test_the_pre_2016_close_tail_is_registered_as_a_known_gap():
    """本库 2016 前最后一根 bar 是 14:59，官方到 15:15 ⇒ 15:00–15:14 是授权缺席。"""
    assert expected_absent_minutes(date(2011, 6, 1)) == frozenset(range(900, 915))


def test_the_late_era_has_no_known_gap():
    assert expected_absent_minutes(date(2016, 6, 1)) == frozenset()


def test_the_known_gap_ends_with_the_early_era():
    assert expected_absent_minutes(date(2015, 12, 31)) == frozenset(range(900, 915))
    assert expected_absent_minutes(date(2016, 1, 4)) == frozenset()


def test_a_known_gap_carries_a_disclosable_reason():
    """缺口要进保真度报告，所以登记项必须自带人话理由，不能只有一对分钟数。"""
    gaps = known_gaps_on(date(2011, 6, 1))

    assert len(gaps) == 1
    assert "14:59" in gaps[0].reason and "15:15" in gaps[0].reason


def test_the_early_era_archive_count_is_the_official_length_minus_the_gap():
    """270 官方分钟 − 15 分钟授权缺席 = 255，与实测抽样的库内根数一致。"""
    slots, _ = _slots("IF", date(2011, 6, 1), date(2011, 5, 31))

    assert len(slots) - len(expected_absent_minutes(date(2011, 6, 1))) == 255


# --------------------------------------------------------------------------
# 5. 时间戳映射硬失败
# --------------------------------------------------------------------------


def _at(trade_date, hhmm):
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(
        trade_date.year, trade_date.month, trade_date.day, hour, minute, tzinfo=SHANGHAI
    )


def test_bars_that_land_on_slots_are_accepted():
    trade_date = date(2016, 6, 1)
    slots, rule = _slots("IF", trade_date, date(2016, 5, 31))

    require_bars_map_to_slots(
        bar_times=[_at(trade_date, "09:30"), _at(trade_date, "14:59")],
        slots=slots,
        rule=rule,
    )


def test_a_bar_inside_the_lunch_break_is_a_hard_failure():
    trade_date = date(2016, 6, 1)
    slots, rule = _slots("IF", trade_date, date(2016, 5, 31))

    with pytest.raises(SessionClockError, match="session_bar_unmapped"):
        require_bars_map_to_slots(
            bar_times=[_at(trade_date, "12:00")], slots=slots, rule=rule
        )


def test_a_late_era_bar_at_1505_is_a_hard_failure():
    """晚年代 15:00 收盘。15:05 的 bar 映射不到任何时段，不许当成尾巴收下。"""
    trade_date = date(2016, 6, 1)
    slots, rule = _slots("IF", trade_date, date(2016, 5, 31))

    with pytest.raises(SessionClockError, match="session_bar_unmapped"):
        require_bars_map_to_slots(
            bar_times=[_at(trade_date, "15:05")], slots=slots, rule=rule
        )


def test_a_duplicate_bar_timestamp_is_a_hard_failure():
    """唯一性是「映射到唯一时段」的一半 —— 同一分钟出现两次也要红。"""
    trade_date = date(2016, 6, 1)
    slots, rule = _slots("IF", trade_date, date(2016, 5, 31))
    twice = [_at(trade_date, "09:30"), _at(trade_date, "09:30")]

    with pytest.raises(SessionClockError, match="session_bar_duplicate"):
        require_bars_map_to_slots(bar_times=twice, slots=slots, rule=rule)


def test_a_naive_bar_timestamp_is_a_hard_failure():
    """时区裸时间戳会静默偏 8 小时，必须当场拒绝而不是比较失败。"""
    trade_date = date(2016, 6, 1)
    slots, rule = _slots("IF", trade_date, date(2016, 5, 31))
    naive = datetime(2016, 6, 1, 9, 30)

    with pytest.raises(SessionClockError, match="session_bar_naive"):
        require_bars_map_to_slots(bar_times=[naive], slots=slots, rule=rule)


# --------------------------------------------------------------------------
# 6. 覆盖度闸
# --------------------------------------------------------------------------


def _calendar(*days):
    return tuple(days)


def test_a_full_year_passes_the_coverage_gate():
    calendar = _calendar(date(2016, 1, 4), date(2016, 1, 5), date(2016, 1, 6))

    verdict = coverage_gate(
        calendar_dates=calendar,
        observed_dates={"IF": calendar},
        window_start=date(2016, 1, 1),
        window_end=date(2016, 1, 31),
    )

    assert verdict.paper_faithful is True
    assert verdict.shortfalls == ()


def test_one_missing_day_fails_the_gate_by_default():
    calendar = _calendar(date(2016, 1, 4), date(2016, 1, 5), date(2016, 1, 6))

    verdict = coverage_gate(
        calendar_dates=calendar,
        observed_dates={"IF": (date(2016, 1, 4), date(2016, 1, 6))},
        window_start=date(2016, 1, 1),
        window_end=date(2016, 1, 31),
    )

    assert verdict.paper_faithful is False
    assert len(verdict.shortfalls) == 1
    shortfall = verdict.shortfalls[0]
    assert (shortfall.product, shortfall.year) == ("IF", 2016)
    assert (shortfall.expected, shortfall.observed) == (3, 2)
    assert shortfall.missing_dates == (date(2016, 1, 5),)


def test_the_threshold_can_be_loosened_but_the_shortfall_is_still_reported():
    """阈值放宽只改 paper_faithful 这一个判定，缺口本身照报不误。"""
    calendar = _calendar(date(2016, 1, 4), date(2016, 1, 5), date(2016, 1, 6))

    verdict = coverage_gate(
        calendar_dates=calendar,
        observed_dates={"IF": (date(2016, 1, 4), date(2016, 1, 6))},
        window_start=date(2016, 1, 1),
        window_end=date(2016, 1, 31),
        max_missing_days=1,
    )

    assert verdict.paper_faithful is True
    assert len(verdict.shortfalls) == 1


def test_a_product_is_only_expected_from_its_listing_day():
    """IM 2022-07-22 才挂牌。拿它上市前的交易日算缺口，是把闸门调成必红。"""
    calendar = _calendar(date(2022, 7, 21), date(2022, 7, 22), date(2022, 7, 25))

    verdict = coverage_gate(
        calendar_dates=calendar,
        observed_dates={"IM": (date(2022, 7, 22), date(2022, 7, 25))},
        window_start=date(2022, 1, 1),
        window_end=date(2022, 12, 31),
    )

    assert verdict.paper_faithful is True


def test_years_are_reported_separately():
    calendar = _calendar(date(2016, 12, 30), date(2017, 1, 3), date(2017, 1, 4))

    verdict = coverage_gate(
        calendar_dates=calendar,
        observed_dates={"IF": (date(2016, 12, 30), date(2017, 1, 3))},
        window_start=date(2016, 1, 1),
        window_end=date(2017, 12, 31),
    )

    assert [(s.year, s.observed, s.expected) for s in verdict.shortfalls] == [
        (2017, 1, 2)
    ]


def test_an_observed_day_outside_the_exchange_calendar_is_a_hard_failure():
    """光数根数会被"日期对不上但个数凑够了"骗过去，所以先要求观测日属于日历。"""
    calendar = _calendar(date(2016, 1, 4), date(2016, 1, 5))

    with pytest.raises(ValueError, match="coverage_observed_not_in_calendar"):
        coverage_gate(
            calendar_dates=calendar,
            observed_dates={"IF": (date(2016, 1, 4), date(2016, 1, 9))},
            window_start=date(2016, 1, 1),
            window_end=date(2016, 1, 31),
        )


def test_an_unknown_product_is_a_hard_failure():
    with pytest.raises(ValueError, match="coverage_unknown_product"):
        coverage_gate(
            calendar_dates=_calendar(date(2016, 1, 4)),
            observed_dates={"RB": (date(2016, 1, 4),)},
            window_start=date(2016, 1, 1),
            window_end=date(2016, 1, 31),
        )
