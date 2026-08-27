"""15 分钟面板与成交价缓存（计划 Task 3）。

面板每行 = 一个 `(product, slot_end)`：15 分钟 K 线 + **下** 5 分钟 VWAP 成交价。
把成交价一起缓存下来，后面 12 次网格回测就再也不用过网。
"""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

pytest_plugins = ("tests.commodity_fixtures",)

from common.commodity import panel as commodity_panel  # noqa: E402
from common.commodity.panel import (  # noqa: E402
    PANEL_COLUMNS,
    build_contexts,
    build_panel,
    build_session_bars,
    normalise_panel,
    resolve_pending_fill,
)
from common.minute.sessions import (
    SessionRule,
    SessionSegment,
    build_trading_slots,
    fifteen_minute_buckets,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2024, 3, 5)
PREVIOUS = date(2024, 3, 4)
CONTRACT = "RB2405"


def _day_only_rule(exchange="SHFE", product="RB"):
    """只有日盘的规则：09:00-10:15 / 10:30-11:30 / 13:30-15:00，共 15 个 15 分钟桶。"""
    return SessionRule.day_only(exchange, product, version="commodity-v1")


def _frame(
    slots,
    *,
    silent=(),
    price=100.0,
    symbol=CONTRACT,
    trade_date=None,
    daily_contract=None,
):
    """替身分钟帧。

    ⚠️ 列必须与 `common.minute.pg_source.build_minute_batch_query` 真实返回的一致：
    它带着由候选表 join 出来的 `trade_date` / `product` / `daily_contract`，而面板
    正是靠 `trade_date` 把夜盘切到**归属交易日**上。替身少一列，回测层就会在真库上
    才炸。
    """
    rows = []
    for index, slot in enumerate(slots):
        traded = index not in silent
        level = price + index
        rows.append(
            {
                "trade_date": trade_date,
                "product": "RB",
                "daily_contract": daily_contract or f"{symbol}.SHF",
                "bar_time": slot,
                "symbol": symbol,
                "open": level,
                "high": level + 0.5,
                "low": level - 0.5,
                "close": level,
                "volume": 10.0 if traded else 0.0,
                "open_interest": 100.0 + index,
                "amount": (level * 10.0 * 10) if traded else 0.0,
            }
        )
    return pd.DataFrame(rows)


def test_panel_carries_oi_and_actual_fill_time(session, minute_frame):
    rows = build_session_bars(
        minute_frame,
        slots=session.slots,
        buckets=session.buckets,
        contract="RB2405.SHF",
        multiplier=10,
    )
    assert "open_interest" in PANEL_COLUMNS
    assert "fill_time" in PANEL_COLUMNS
    traded = minute_frame.loc[
        minute_frame["bar_time"].isin(session.buckets[0]) & (minute_frame["volume"] > 0)
    ]
    assert rows[0]["open_interest"] == traded["open_interest"].iloc[-1]
    assert pd.Timestamp(rows[0]["fill_time"]) > pd.Timestamp(rows[0]["slot_end"])


def test_panel_takes_open_interest_from_the_chronologically_last_trade(
    session, minute_frame
):
    shuffled = minute_frame.iloc[::-1].reset_index(drop=True)
    rows = build_session_bars(
        shuffled,
        slots=session.slots,
        buckets=session.buckets,
        contract="RB2405.SHF",
        multiplier=10,
    )
    assert rows[0]["open_interest"] == 114.0


@pytest.mark.parametrize(
    "last_open_interest", [None, float("nan"), float("inf"), float("-inf")]
)
def test_panel_does_not_fall_back_when_the_last_traded_oi_is_not_finite(
    session, minute_frame, last_open_interest
):
    minute_frame["open_interest"] = minute_frame["open_interest"].astype(object)
    minute_frame.loc[14, "open_interest"] = last_open_interest
    rows = build_session_bars(
        minute_frame,
        slots=session.slots,
        buckets=session.buckets,
        contract="RB2405.SHF",
        multiplier=10,
    )
    assert rows[0]["open_interest"] is None


def test_panel_emits_no_open_interest_when_every_traded_oi_is_null(
    session, minute_frame
):
    minute_frame["open_interest"] = minute_frame["open_interest"].astype(object)
    minute_frame.loc[:14, "open_interest"] = None
    rows = build_session_bars(
        minute_frame,
        slots=session.slots,
        buckets=session.buckets,
        contract="RB2405.SHF",
        multiplier=10,
    )
    assert rows[0]["open_interest"] is None


def test_panel_drops_zero_volume_minutes_before_aggregating(session):
    """空 K 线带的是前收结转价而非成交价，直接聚合会造出根本没成交过的极值。"""
    rule, slots, buckets = session.rule, session.slots, session.buckets
    frame = _frame(slots)
    # 把第 3 分钟做成一根价格离谱的空 K 线。
    frame.loc[3, ["open", "high", "low", "close"]] = 9999.0
    frame.loc[3, "volume"] = 0.0
    bars = build_session_bars(
        frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
    )
    assert bars[0]["high"] < 9999.0


def test_a_bucket_with_no_traded_minute_is_marked_not_fabricated(session):
    rule, slots, buckets = session.rule, session.slots, session.buckets
    frame = _frame(slots, silent=range(15))
    bars = build_session_bars(
        frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
    )
    assert bars[0]["no_trade"] is True
    assert bars[0]["close"] is None
    assert bars[0]["open_interest"] is None


def test_fill_price_is_the_next_five_minutes_not_the_current_bar(session):
    """成交价是信号触发**之后**的 5 分钟；用当根 bar 自己就是前视。"""
    rule, slots, buckets = session.rule, session.slots, session.buckets
    frame = _frame(slots)
    bars = build_session_bars(
        frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
    )
    # 第 0 根桶覆盖分钟 0..14，其成交窗是分钟 15..19，价格 115..119。
    fill = bars[0]["fill_price"]
    assert fill == pytest.approx(sum(100.0 + i for i in range(15, 20)) / 5)
    assert fill > bars[0]["close"]


def test_last_bucket_of_a_session_leaves_the_fill_pending(session):
    """D14：时段最后一根没有『之后 5 分钟』可用，必须挂起而不是就地编一个。"""
    rule, slots, buckets = session.rule, session.slots, session.buckets
    frame = _frame(slots)
    bars = build_session_bars(
        frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
    )
    assert bars[-1]["fill_price"] is None
    assert bars[-1]["fill_time"] is None
    assert bars[-1]["fill_pending"] is True
    assert all(bar["fill_pending"] is False for bar in bars[:-1])


def test_pending_fill_resolves_from_the_next_sessions_first_five_minutes():
    """D14 的另一半：挂起的成交价由**下一时段前 5 分钟**补上。"""
    rule = _day_only_rule()
    next_date, next_previous = date(2024, 3, 6), TRADE_DATE
    next_slots = build_trading_slots(next_date, next_previous, rule)
    next_frame = _frame(next_slots, price=200.0)
    price = resolve_pending_fill(
        next_frame, slots=next_slots, contract=CONTRACT, multiplier=10
    )
    assert price == pytest.approx(sum(200.0 + i for i in range(5)) / 5)


def test_czce_uses_ohlc_typical_pricing_basis(session):
    """郑商所的 amount 是按单一整数价合成的，按它算 VWAP 会得到假成交价。"""
    rule, slots, buckets = session.rule, session.slots, session.buckets
    frame = _frame(slots)
    # 把 amount 做成与 OHLC 无关的假值：amount_vwap 会被它带走，ohlc_typical 不会。
    frame["amount"] = 1.0
    typical = build_session_bars(
        frame,
        slots=slots,
        buckets=buckets,
        contract=CONTRACT,
        multiplier=10,
        pricing_basis="ohlc_typical",
    )
    assert typical[0]["fill_price"] == pytest.approx(
        sum(100.0 + i for i in range(15, 20)) / 5
    )


def test_bars_carry_their_slot_end_and_contract(session):
    rule, slots, buckets = session.rule, session.slots, session.buckets
    frame = _frame(slots)
    bars = build_session_bars(
        frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
    )
    assert bars[0]["contract"] == CONTRACT
    assert bars[0]["slot_end"] == slots[14] + timedelta(minutes=1)
    assert len(bars) == len(buckets)


def test_an_unpriceable_fill_window_yields_no_price_at_all(session):
    """成交窗零成交时不许拿收盘价顶上 —— 那是凭空造一笔没发生过的成交。

    研报没写这种情形；面板只负责把它标出来，裁定交给回测层。
    """
    rule, slots, buckets = session.rule, session.slots, session.buckets
    frame = _frame(slots, silent=range(15, 20))  # 第 0 根桶的成交窗整段无成交
    bars = build_session_bars(
        frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
    )
    assert bars[0]["fill_price"] is None
    assert bars[0]["fill_time"] == slots[19]
    assert bars[0]["fill_unpriceable"] is True
    assert bars[0]["close"] is not None  # 这根 bar 自己是有成交的
    assert bars[1]["fill_unpriceable"] is False  # 只影响那一根


def test_resolve_pending_fill_returns_none_when_the_next_session_is_silent():
    rule = _day_only_rule()
    next_slots = build_trading_slots(date(2024, 3, 6), TRADE_DATE, rule)
    silent = _frame(next_slots, silent=range(5))
    assert (
        resolve_pending_fill(silent, slots=next_slots, contract=CONTRACT, multiplier=10)
        is None
    )


# --- 逐月编排 ---------------------------------------------------------------

from common.dominant import DominantChoice  # noqa: E402

DAYS = [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)]


def _choices(contract="RB2405.SHF"):
    return [
        DominantChoice(
            trade_date=day,
            product="RB",
            contract=contract,
            oi=1,
            volume=1,
            selected_from=day,
        )
        for day in DAYS
    ]


class _FakeSource:
    """按月吐分钟行的确定性替身，价格逐日抬一档以便认出是哪一天。"""

    def __init__(self, contexts, *, silent_opening_dates=()):
        self.contexts = contexts
        self.months = 0
        self.silent_opening_dates = set(silent_opening_dates)

    def iter_month(self, candidates, lower, upper):
        self.months += 1
        frames = []
        for candidate in candidates:
            context = self.contexts[(candidate.trade_date, candidate.product)]
            base = 100.0 + 1000.0 * DAYS.index(candidate.trade_date)
            frames.append(
                _frame(
                    context.slots,
                    silent=(
                        range(5)
                        if candidate.trade_date in self.silent_opening_dates
                        else ()
                    ),
                    price=base,
                    symbol=candidate.minute_symbol,
                    trade_date=candidate.trade_date,
                    daily_contract=candidate.daily_contract,
                )
            )
        if frames:
            yield pd.concat(frames, ignore_index=True)


def _panel(*, silent_opening_dates=()):
    rules = [_day_only_rule()]
    contexts = build_contexts(_choices(), rules=rules)
    source = _FakeSource(contexts, silent_opening_dates=silent_opening_dates)
    factors = {key: 1.0 + index for index, key in enumerate(sorted(contexts))}
    return contexts, build_panel(
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange={},
        multiplier_resolver=lambda candidate, frame: 10,
        adjustment_factor_by_key=factors,
    )


def test_contexts_skip_the_first_day_because_night_slots_need_a_previous_session():
    contexts = build_contexts(_choices(), rules=[_day_only_rule()])
    assert sorted(key[0] for key in contexts) == DAYS[1:]


def test_a_pending_fill_is_resolved_from_the_products_next_session():
    """D14 跨日接力：某日最后一根桶的成交价来自**下一交易日**的前 5 分钟。"""
    contexts, panel = _panel()
    last_of_day_two = panel.loc[panel["trade_date"] == pd.Timestamp(DAYS[1])].iloc[-1]
    # 替身给第三天（DAYS[2]）的基准价是 2100，它的前五分钟是 2100..2104。
    expected = sum(2100.0 + i for i in range(5)) / 5
    assert bool(last_of_day_two["fill_pending"]) is False
    assert last_of_day_two["fill_price"] == pytest.approx(expected)
    assert last_of_day_two["fill_time"] == contexts[(DAYS[2], "RB")].slots[4]


def test_an_unpriceable_cross_session_fill_records_the_attempted_window_end():
    contexts, panel = _panel(silent_opening_dates=(DAYS[2],))
    last_of_day_two = panel.loc[panel["trade_date"] == pd.Timestamp(DAYS[1])].iloc[-1]
    assert pd.isna(last_of_day_two["fill_price"])
    assert bool(last_of_day_two["fill_unpriceable"]) is True
    assert last_of_day_two["fill_time"] == contexts[(DAYS[2], "RB")].slots[4]


def test_the_final_bar_of_the_whole_panel_stays_pending():
    """最后一天之后没有下一时段，那一根只能挂着 —— 不许拿别的价顶上。"""
    _, panel = _panel()
    assert bool(panel.iloc[-1]["fill_pending"]) is True
    # pandas 把 None 放进浮点列会变成 NaN；下游据此判"没有成交价"。
    assert pd.isna(panel.iloc[-1]["fill_price"])


def test_panel_covers_every_bucket_of_every_context():
    contexts, panel = _panel()
    expected = sum(len(context.buckets) for context in contexts.values())
    assert len(panel) == expected


def test_panel_uses_daily_concrete_contract_ids_with_exchange_suffix():
    _, panel = _panel()

    assert set(panel["contract"]) == {"RB2405.SHF"}


def test_panel_resolves_date_effective_multiplier_for_every_product_day():
    contexts = build_contexts(_choices(), rules=[_day_only_rule()])
    calls = []

    def resolve(candidate, frame):
        calls.append((candidate.trade_date, candidate.daily_contract, len(frame)))
        return 10

    build_panel(
        contexts=contexts,
        source=_FakeSource(contexts),
        pricing_basis_by_exchange={},
        multiplier_resolver=resolve,
        adjustment_factor_by_key={key: 1.0 for key in contexts},
    )

    assert [call[:2] for call in calls] == [
        (day, "RB2405.SHF") for day in sorted(key[0] for key in contexts)
    ]
    assert all(call[2] > 0 for call in calls)


class _OmittingSource:
    def __init__(self, contexts, missing_dates):
        self.contexts = contexts
        self.missing_dates = set(missing_dates)

    def iter_month(self, candidates, lower, upper):
        frames = []
        for candidate in candidates:
            if candidate.trade_date in self.missing_dates:
                continue
            context = self.contexts[(candidate.trade_date, candidate.product)]
            frames.append(
                _frame(
                    context.slots,
                    price=100.0,
                    symbol=candidate.minute_symbol,
                    trade_date=candidate.trade_date,
                    daily_contract=candidate.daily_contract,
                )
            )
        if frames:
            yield pd.concat(frames, ignore_index=True)


def _panel_with_missing_context(days, missing_date):
    choices = [
        DominantChoice(
            trade_date=day,
            product="RB",
            contract="RB2405.SHF",
            oi=1,
            volume=1,
            selected_from=day,
        )
        for day in days
    ]
    contexts = build_contexts(choices, rules=[_day_only_rule()])
    return build_panel(
        contexts=contexts,
        source=_OmittingSource(contexts, [missing_date]),
        pricing_basis_by_exchange={},
        multiplier_resolver=lambda candidate, frame: 10,
        adjustment_factor_by_key={key: 1.0 for key in contexts},
    )


def test_panel_refuses_a_missing_middle_product_day_context():
    days = [date(2024, 3, day) for day in (4, 5, 6, 7)]

    with pytest.raises(ValueError, match="panel_context_missing.*2024-03-06"):
        _panel_with_missing_context(days, date(2024, 3, 6))


def test_panel_refuses_a_missing_final_product_day_context():
    with pytest.raises(ValueError, match="panel_context_missing.*2024-03-06"):
        _panel_with_missing_context(DAYS, DAYS[-1])


def test_panel_carries_the_product_days_adjustment_factor():
    contexts, panel = _panel()
    expected = {key: 1.0 + index for index, key in enumerate(sorted(contexts))}
    observed = (
        panel.loc[:, ["trade_date", "product", "adj_factor"]]
        .drop_duplicates()
        .assign(trade_date=lambda frame: frame["trade_date"].dt.date)
    )
    assert {
        (row.trade_date, row.product): row.adj_factor
        for row in observed.itertuples(index=False)
    } == expected


def test_panel_refuses_a_missing_adjustment_factor():
    contexts = build_contexts(_choices(), rules=[_day_only_rule()])
    source = _FakeSource(contexts)

    with pytest.raises(ValueError, match="panel_adjustment_factor_missing"):
        build_panel(
            contexts=contexts,
            source=source,
            pricing_basis_by_exchange={},
            multiplier_resolver=lambda candidate, frame: 10,
            adjustment_factor_by_key={},
        )


def test_contexts_do_not_depend_on_the_order_choices_arrive_in():
    """前一交易日是逐品种推出来的；输入乱序时不排序就会把夜盘挂到错误的日子上。"""
    ordered = build_contexts(_choices(), rules=[_day_only_rule()])
    shuffled = build_contexts(list(reversed(_choices())), rules=[_day_only_rule()])
    assert sorted(ordered) == sorted(shuffled)
    for key in ordered:
        assert ordered[key].slots == shuffled[key].slots


def test_context_after_a_dominant_gap_uses_its_causal_source_session():
    """主力空档后的夜盘属于换月判定日前夜，不属于上一个有效主力日的前夜。"""
    predecessor = DominantChoice(
        date(2024, 3, 4), "RB", "RB2405.SHF", 1, 1, date(2024, 3, 1)
    )
    resumed = DominantChoice(
        date(2024, 3, 11), "RB", "RB2410.SHF", 1, 1, date(2024, 3, 8)
    )

    night_rule = SessionRule(
        exchange="SHFE",
        product="RB",
        effective_start=date(2020, 1, 1),
        effective_end=None,
        segments=(
            SessionSegment(-180, -120),
            *_day_only_rule().segments,
        ),
        version="commodity-v1",
    )
    contexts = build_contexts((predecessor, resumed), rules=[night_rule])

    assert contexts[(resumed.trade_date, "RB")].slots[0].date() == date(2024, 3, 8)


def test_month_context_choices_keep_only_the_last_predecessor_per_product():
    """预热主力链只提供前态；不得让旧月份去解析本月策略并不需要的时段规则。"""
    old = date(2022, 1, 6)
    predecessor = date(2022, 12, 30)
    current = date(2023, 1, 3)
    choices = [
        DominantChoice(old, "LU", "LU2205.INE", 1, 1, old),
        DominantChoice(predecessor, "LU", "LU2303.INE", 1, 1, predecessor),
        DominantChoice(current, "LU", "LU2303.INE", 1, 1, current),
        DominantChoice(predecessor, "RB", "RB2305.SHF", 1, 1, predecessor),
        DominantChoice(current, "RB", "RB2305.SHF", 1, 1, current),
    ]

    selected = commodity_panel.context_choices_for_month(
        choices, month_start=date(2023, 1, 1)
    )

    assert {(choice.trade_date, choice.product) for choice in selected} == {
        (predecessor, "LU"),
        (predecessor, "RB"),
        (current, "LU"),
        (current, "RB"),
    }


def test_panel_round_trips_through_parquet(tmp_path):
    """面板是要落盘缓存的 —— 写不出去等于每跑一个网格点都要重拉一次分钟数据。

    ⚠️ 本仓只装了 fastparquet（没有 pyarrow），它**推不出 object 列的类型**：
    无成交 bar 的 O/H/L/C 和发不出的成交价都是 None，不定死列类型就写不出去。
    """
    _, panel = _panel()
    path = tmp_path / "panel.parquet"
    panel.to_parquet(path, index=False)
    restored = pd.read_parquet(path)
    assert list(restored.columns) == list(panel.columns)
    assert len(restored) == len(panel)
    assert restored["close"].dtype == "float64"
    assert restored["open_interest"].dtype == "float64"
    assert restored["no_trade"].dtype == "bool"
    # 单位必须是 ns：datetime64[s] 写得出去但读不回来。
    assert str(restored["trade_date"].dtype) == "datetime64[ns]"
    assert str(restored["slot_end"].dtype) == "datetime64[ns, Asia/Shanghai]"
    assert str(restored["fill_time"].dtype) == "datetime64[ns, Asia/Shanghai]"
    pd.testing.assert_series_equal(restored["fill_time"], panel["fill_time"])


def test_empty_panel_has_the_normalised_schema():
    panel = build_panel(
        contexts={},
        source=object(),
        pricing_basis_by_exchange={},
        multiplier_resolver=lambda candidate, frame: 10,
        adjustment_factor_by_key={},
    )
    assert list(panel.columns) == list(PANEL_COLUMNS)
    assert panel["open_interest"].dtype == "float64"
    assert panel["fill_pending"].dtype == "bool"
    assert panel["product"].dtype == "string"
    assert panel["multiplier"].dtype == "int64"
    assert str(panel["slot_end"].dtype) == "datetime64[ns, Asia/Shanghai]"
    assert str(panel["fill_time"].dtype) == "datetime64[ns, Asia/Shanghai]"


def test_normalise_panel_rejects_naive_fill_times():
    _, panel = _panel()
    naive = panel.copy()
    naive["fill_time"] = naive["fill_time"].dt.tz_localize(None)

    with pytest.raises(ValueError, match="fill_time.*timezone-aware"):
        normalise_panel(naive)


def test_normalise_panel_preserves_timezone_dtype_for_all_nat_fill_times():
    _, panel = _panel()
    panel["fill_time"] = pd.NaT

    normalised = normalise_panel(panel)

    assert normalised["fill_time"].isna().all()
    assert str(normalised["fill_time"].dtype) == "datetime64[ns, Asia/Shanghai]"


def test_a_no_trade_bar_reads_as_nan_not_zero():
    """无成交 bar 的价格必须是 NaN。0 会被下游当成一个真价格拿去算收益。"""
    rule = _day_only_rule()
    slots = build_trading_slots(TRADE_DATE, PREVIOUS, rule)
    buckets = fifteen_minute_buckets(slots, rule)
    frame = _frame(slots, silent=range(15), trade_date=TRADE_DATE)
    panel = normalise_panel(
        pd.DataFrame(
            build_session_bars(
                frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
            )
        )
    )
    assert pd.isna(panel.iloc[0]["close"])
    assert panel.iloc[0]["volume"] == 0.0
