"""15 分钟面板与成交价缓存（计划 Task 3）。

面板每行 = 一个 `(product, slot_end)`：15 分钟 K 线 + **下** 5 分钟 VWAP 成交价。
把成交价一起缓存下来，后面 12 次网格回测就再也不用过网。
"""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from common.minute.sessions import (
    SessionRule,
    SessionSegment,
    build_trading_slots,
    fifteen_minute_buckets,
)
from cta_continuous.panel import build_session_bars, resolve_pending_fill

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2024, 3, 5)
PREVIOUS = date(2024, 3, 4)
CONTRACT = "RB2405"


def _day_only_rule(exchange="SHFE", product="RB"):
    """只有日盘的规则：09:00-10:15 / 10:30-11:30 / 13:30-15:00，共 15 个 15 分钟桶。"""
    return SessionRule.day_only(exchange, product, version="commodity-v1")


def _frame(
    slots, *, silent=(), price=100.0, symbol=CONTRACT,
    trade_date=None, daily_contract=None,
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
                "amount": (level * 10.0 * 10) if traded else 0.0,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture
def session():
    rule = _day_only_rule()
    slots = build_trading_slots(TRADE_DATE, PREVIOUS, rule)
    return rule, slots, fifteen_minute_buckets(slots, rule)


def test_panel_drops_zero_volume_minutes_before_aggregating(session):
    """空 K 线带的是前收结转价而非成交价，直接聚合会造出根本没成交过的极值。"""
    rule, slots, buckets = session
    frame = _frame(slots)
    # 把第 3 分钟做成一根价格离谱的空 K 线。
    frame.loc[3, ["open", "high", "low", "close"]] = 9999.0
    frame.loc[3, "volume"] = 0.0
    bars = build_session_bars(
        frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
    )
    assert bars[0]["high"] < 9999.0


def test_a_bucket_with_no_traded_minute_is_marked_not_fabricated(session):
    rule, slots, buckets = session
    frame = _frame(slots, silent=range(15))
    bars = build_session_bars(
        frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
    )
    assert bars[0]["no_trade"] is True
    assert bars[0]["close"] is None


def test_fill_price_is_the_next_five_minutes_not_the_current_bar(session):
    """成交价是信号触发**之后**的 5 分钟；用当根 bar 自己就是前视。"""
    rule, slots, buckets = session
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
    rule, slots, buckets = session
    frame = _frame(slots)
    bars = build_session_bars(
        frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
    )
    assert bars[-1]["fill_price"] is None
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
    rule, slots, buckets = session
    frame = _frame(slots)
    # 把 amount 做成与 OHLC 无关的假值：amount_vwap 会被它带走，ohlc_typical 不会。
    frame["amount"] = 1.0
    typical = build_session_bars(
        frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10,
        pricing_basis="ohlc_typical",
    )
    assert typical[0]["fill_price"] == pytest.approx(
        sum(100.0 + i for i in range(15, 20)) / 5
    )


def test_bars_carry_their_slot_end_and_contract(session):
    rule, slots, buckets = session
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
    rule, slots, buckets = session
    frame = _frame(slots, silent=range(15, 20))     # 第 0 根桶的成交窗整段无成交
    bars = build_session_bars(
        frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
    )
    assert bars[0]["fill_price"] is None
    assert bars[0]["fill_unpriceable"] is True
    assert bars[0]["close"] is not None          # 这根 bar 自己是有成交的
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
import cta_continuous.panel as continuous_panel  # noqa: E402
from cta_continuous.panel import build_contexts, build_panel  # noqa: E402

DAYS = [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)]


def _choices(contract="RB2405.SHF"):
    return [
        DominantChoice(
            trade_date=day, product="RB", contract=contract,
            oi=1, volume=1, selected_from=day,
        )
        for day in DAYS
    ]


class _FakeSource:
    """按月吐分钟行的确定性替身，价格逐日抬一档以便认出是哪一天。"""

    def __init__(self, contexts):
        self.contexts = contexts
        self.months = 0

    def iter_month(self, candidates, lower, upper):
        self.months += 1
        frames = []
        for candidate in candidates:
            context = self.contexts[(candidate.trade_date, candidate.product)]
            base = 100.0 + 1000.0 * DAYS.index(candidate.trade_date)
            frames.append(
                _frame(
                    context.slots,
                    price=base,
                    symbol=candidate.minute_symbol,
                    trade_date=candidate.trade_date,
                    daily_contract=candidate.daily_contract,
                )
            )
        if frames:
            yield pd.concat(frames, ignore_index=True)


def _panel(segments=None):
    rules = [_day_only_rule()]
    contexts = build_contexts(_choices(), rules=rules)
    source = _FakeSource(contexts)
    factors = {key: 1.0 + index for index, key in enumerate(sorted(contexts))}
    if segments is None:
        segments = {key: 0 for key in contexts}
    return contexts, build_panel(
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange={},
        multiplier_resolver=lambda candidate, frame: 10,
        adjustment_factor_by_key=factors,
        continuity_segment_by_key=segments,
    )


def _assert_panel_schema(panel):
    assert list(panel.columns) == list(continuous_panel.PANEL_COLUMNS)
    assert {column: str(dtype) for column, dtype in panel.dtypes.items()} == {
        "product": "string",
        "contract": "string",
        "trade_date": "datetime64[ns]",
        "slot_end": "datetime64[ns, Asia/Shanghai]",
        "open": "float64",
        "high": "float64",
        "low": "float64",
        "close": "float64",
        "volume": "float64",
        "no_trade": "bool",
        "adj_factor": "float64",
        "continuity_segment": "int64",
        "fill_price": "float64",
        "fill_pending": "bool",
        "fill_unpriceable": "bool",
        "pricing_basis": "string",
        "multiplier": "int64",
    }


def test_empty_contexts_return_a_typed_parquet_safe_panel(tmp_path):
    panel = build_panel(
        contexts={},
        source=object(),
        pricing_basis_by_exchange={},
        multiplier_resolver=lambda candidate, frame: 10,
        adjustment_factor_by_key={},
        continuity_segment_by_key={},
    )

    assert panel.empty
    _assert_panel_schema(panel)
    path = tmp_path / "empty-panel.parquet"
    panel.to_parquet(path, index=False)
    restored = pd.read_parquet(path)
    assert restored.empty
    assert list(restored.columns) == list(continuous_panel.PANEL_COLUMNS)


def test_contexts_without_source_rows_return_a_typed_empty_panel():
    contexts = build_contexts(_choices(), rules=[_day_only_rule()])
    factors = {key: 1.0 + index for index, key in enumerate(sorted(contexts))}
    segments = {key: index for index, key in enumerate(sorted(contexts))}

    class EmptySource:
        @staticmethod
        def iter_month(candidates, lower, upper):
            return iter(())

    panel = build_panel(
        contexts=contexts,
        source=EmptySource(),
        pricing_basis_by_exchange={},
        multiplier_resolver=lambda candidate, frame: 10,
        adjustment_factor_by_key=factors,
        continuity_segment_by_key=segments,
    )

    assert panel.empty
    _assert_panel_schema(panel)


def test_contexts_skip_the_first_day_because_night_slots_need_a_previous_session():
    contexts = build_contexts(_choices(), rules=[_day_only_rule()])
    assert sorted(key[0] for key in contexts) == DAYS[1:]


def test_a_pending_fill_is_resolved_from_the_products_next_session():
    """D14 跨日接力：某日最后一根桶的成交价来自**下一交易日**的前 5 分钟。"""
    _, panel = _panel()
    last_of_day_two = panel.loc[panel["trade_date"] == pd.Timestamp(DAYS[1])].iloc[-1]
    # 替身给第三天（DAYS[2]）的基准价是 2100，它的前五分钟是 2100..2104。
    expected = sum(2100.0 + i for i in range(5)) / 5
    assert bool(last_of_day_two["fill_pending"]) is False
    assert last_of_day_two["fill_price"] == pytest.approx(expected)


def test_a_pending_fill_stops_at_a_continuity_segment_break():
    segments = {
        (day, "RB"): index
        for index, day in enumerate(DAYS[1:])
    }
    _, panel = _panel(segments=segments)

    old_segment_last = panel.loc[
        panel["trade_date"] == pd.Timestamp(DAYS[1])
    ].iloc[-1]
    assert bool(old_segment_last["fill_pending"]) is False
    assert pd.isna(old_segment_last["fill_price"])
    assert bool(old_segment_last["fill_unpriceable"]) is True

    new_segment = panel.loc[panel["trade_date"] == pd.Timestamp(DAYS[2])]
    assert not new_segment.empty
    assert set(new_segment["continuity_segment"]) == {1}


def test_a_resolve_only_context_prices_the_tail_without_emitting_bars():
    """跨分片接力：只用来定价、不发 bar 的上下文。

    分片是**逐月各调一次** `build_panel` 写出来的，所以 `pending` 字典每片都从空
    开始，月末那一根永远补不上。补法是把下个月首日的上下文一并交进来 —— 但它的
    bar 属于下一个分片，这里发出来就会重复。
    """
    rules = [_day_only_rule()]
    contexts = build_contexts(_choices(), rules=rules)
    tail = (DAYS[2], "RB")
    assert tail in contexts

    panel = build_panel(
        contexts=contexts,
        source=_FakeSource(contexts),
        pricing_basis_by_exchange={},
        multiplier_resolver=lambda candidate, frame: 10,
        adjustment_factor_by_key={key: 1.0 for key in contexts},
        continuity_segment_by_key={key: 0 for key in contexts},
        resolve_only_keys={tail},
    )

    assert set(panel["trade_date"]) == {pd.Timestamp(DAYS[1])}
    last = panel.iloc[-1]
    assert bool(last["fill_pending"]) is False
    assert last["fill_price"] == pytest.approx(sum(2100.0 + i for i in range(5)) / 5)


def test_a_resolve_only_context_does_not_become_the_next_pending_tail():
    """定价用的那一天不能反过来把自己挂起 —— 否则下一分片会去补一根不存在的 bar。"""
    rules = [_day_only_rule()]
    contexts = build_contexts(_choices(), rules=rules)

    panel = build_panel(
        contexts=contexts,
        source=_FakeSource(contexts),
        pricing_basis_by_exchange={},
        multiplier_resolver=lambda candidate, frame: 10,
        adjustment_factor_by_key={key: 1.0 for key in contexts},
        continuity_segment_by_key={key: 0 for key in contexts},
        resolve_only_keys={(DAYS[2], "RB")},
    )

    assert not panel["fill_pending"].any()


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


def test_panel_carries_the_product_days_continuity_segment():
    segments = {
        (day, "RB"): index
        for index, day in enumerate(DAYS[1:])
    }
    contexts, panel = _panel(segments=segments)
    observed = (
        panel[["trade_date", "product", "continuity_segment"]]
        .drop_duplicates()
        .assign(trade_date=lambda frame: frame["trade_date"].dt.date)
    )
    expected = {key: index for index, key in enumerate(sorted(contexts))}
    assert {
        (row.trade_date, row.product): row.continuity_segment
        for row in observed.itertuples(index=False)
    } == expected


def test_panel_refuses_a_missing_adjustment_factor():
    contexts = build_contexts(_choices(), rules=[_day_only_rule()])
    source = _FakeSource(contexts)
    segments = {key: index for index, key in enumerate(sorted(contexts))}

    with pytest.raises(ValueError, match="panel_adjustment_factor_missing"):
        build_panel(
            contexts=contexts,
            source=source,
            pricing_basis_by_exchange={},
            multiplier_resolver=lambda candidate, frame: 10,
            adjustment_factor_by_key={},
            continuity_segment_by_key=segments,
        )


def test_panel_refuses_a_missing_continuity_segment():
    contexts = build_contexts(_choices(), rules=[_day_only_rule()])
    source = _FakeSource(contexts)
    factors = {key: 1.0 + index for index, key in enumerate(sorted(contexts))}
    missing = sorted(contexts)[1:]
    segments = {
        key: index
        for index, key in enumerate(sorted(contexts))
        if key not in missing
    }

    with pytest.raises(ValueError) as caught:
        build_panel(
            contexts=contexts,
            source=source,
            pricing_basis_by_exchange={},
            multiplier_resolver=lambda candidate, frame: 10,
            adjustment_factor_by_key=factors,
            continuity_segment_by_key=segments,
        )
    assert str(caught.value) == (
        "panel_continuity_segment_missing: 缺少品种日连续分段；"
        f"first={missing[0]!r} count={len(missing)}"
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


def test_a_single_target_month_choice_uses_selected_from_without_a_predecessor():
    selected_from = date(2022, 12, 30)
    current = DominantChoice(
        date(2023, 1, 3), "RB", "RB2305.SHF", 1, 1, selected_from
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

    contexts = build_contexts((current,), rules=[night_rule])

    context = contexts[(current.trade_date, "RB")]
    assert context.slots[0].date() == selected_from


def test_month_context_choices_keep_only_sorted_target_month_choices():
    """预热主力链不得让旧月份去解析目标月不需要的时段规则。"""
    old = date(2022, 1, 6)
    predecessor = date(2022, 12, 30)
    current = date(2023, 1, 3)
    choices = [
        DominantChoice(old, "LU", "LU2205.INE", 1, 1, old),
        DominantChoice(predecessor, "LU", "LU2303.INE", 1, 1, predecessor),
        DominantChoice(current, "LU", "LU2303.INE", 1, 1, predecessor),
        DominantChoice(predecessor, "RB", "RB2305.SHF", 1, 1, predecessor),
        DominantChoice(current, "RB", "RB2305.SHF", 1, 1, predecessor),
    ]

    selected = continuous_panel.context_choices_for_month(
        choices, month_start=date(2023, 1, 1)
    )

    assert [(choice.trade_date, choice.product) for choice in selected] == [
        (current, "LU"),
        (current, "RB"),
    ]


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
    assert restored["no_trade"].dtype == "bool"
    assert restored["continuity_segment"].dtype == "int64"
    # 单位必须是 ns：datetime64[s] 写得出去但读不回来。
    assert str(restored["trade_date"].dtype) == "datetime64[ns]"
    assert str(restored["slot_end"].dtype) == "datetime64[ns, Asia/Shanghai]"


def test_a_no_trade_bar_reads_as_nan_not_zero():
    """无成交 bar 的价格必须是 NaN。0 会被下游当成一个真价格拿去算收益。"""
    rule = _day_only_rule()
    slots = build_trading_slots(TRADE_DATE, PREVIOUS, rule)
    buckets = fifteen_minute_buckets(slots, rule)
    frame = _frame(slots, silent=range(15), trade_date=TRADE_DATE)
    from cta_continuous.panel import normalise_panel

    panel = normalise_panel(
        pd.DataFrame(
            build_session_bars(
                frame, slots=slots, buckets=buckets, contract=CONTRACT, multiplier=10
            )
        )
    )
    assert pd.isna(panel.iloc[0]["close"])
    assert panel.iloc[0]["volume"] == 0.0


# --- 覆盖闸 -----------------------------------------------------------------

from common.minute.sessions import SessionClockError  # noqa: E402
from cta_continuous.panel import (  # noqa: E402
    require_session_coverage,
    required_session_keys,
)

MARCH = date(2024, 3, 1)


def test_required_session_keys_match_what_build_contexts_asks_for():
    """闸检查的键与 build_contexts 实际索取的键必须逐点相同 —— 否则闸形同虚设。"""
    choices = _choices()
    contexts = build_contexts(choices, rules=[_day_only_rule()])

    keys = required_session_keys(choices)

    assert set(keys) == {
        ("SHFE", product, trade_date) for trade_date, product in contexts
    }


def test_require_session_coverage_reports_every_uncovered_product_day(tmp_path):
    """不是报第一个就死 —— 一次报全，并落下完整清单。"""
    manifest = tmp_path / "gaps.csv"

    with pytest.raises(SessionClockError) as exc_info:
        require_session_coverage(
            choices=_choices(),
            products_by_month={MARCH: ("RB",)},
            rules=(),
            manifest_path=manifest,
        )

    assert exc_info.value.check == "session_coverage_incomplete"
    # DAYS 有三天，首日没有前一交易日因而不需要规则，故剩两天。
    assert "2 product-days" in str(exc_info.value)
    rows = manifest.read_text(encoding="utf-8").strip().splitlines()
    assert rows[0] == "month,exchange,product,trade_date,found"
    assert rows[1] == "2024-03,SHFE,RB,2024-03-05,0"
    assert rows[2] == "2024-03,SHFE,RB,2024-03-06,0"
    assert len(rows) == 3


def test_require_session_coverage_also_rejects_ambiguous_days(tmp_path):
    """闸要防两边：0 条固然是缺，2 条是资产自相矛盾，同样不能放行。"""
    manifest = tmp_path / "gaps.csv"

    with pytest.raises(SessionClockError) as exc_info:
        require_session_coverage(
            choices=_choices(),
            products_by_month={MARCH: ("RB",)},
            rules=(_day_only_rule(), _day_only_rule()),
            manifest_path=manifest,
        )

    assert exc_info.value.check == "session_coverage_incomplete"
    assert manifest.read_text(encoding="utf-8").strip().splitlines()[1].endswith(",2")


def test_require_session_coverage_ignores_products_outside_the_month_universe(tmp_path):
    """该月宇宙里没有的品种不该被要求具备规则。"""
    require_session_coverage(
        choices=_choices(),
        products_by_month={MARCH: ()},
        rules=(),
        manifest_path=tmp_path / "gaps.csv",
    )

    assert not (tmp_path / "gaps.csv").exists()


def test_require_session_coverage_is_silent_when_every_day_is_covered(tmp_path):
    manifest = tmp_path / "gaps.csv"

    require_session_coverage(
        choices=_choices(),
        products_by_month={MARCH: ("RB",)},
        rules=(_day_only_rule(),),
        manifest_path=manifest,
    )

    assert not manifest.exists()


# --- 接线：闸必须挡在任何分钟查询之前 ---------------------------------------

import importlib.util  # noqa: E402
import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from pathlib import Path  # noqa: E402

_BUILD_PANEL_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "continuous" / "build_panel.py"
)


def _load_build_panel():
    spec = importlib.util.spec_from_file_location(
        "_build_panel_under_test", _BUILD_PANEL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _GateReached(Exception):
    """闸被调用到了 —— 用它把 main() 在闸处截停。"""


def _daily_frame():
    """一个品种、一张合约，覆盖 2023-09 至 2024-03，成交额远超 50 亿门槛。"""
    days = pd.bdate_range("2023-09-01", "2024-03-29").date
    return pd.DataFrame(
        {
            "symbol": ["RB2405.SHF"] * len(days),
            "trade_date": list(days),
            "oi": [10_000] * len(days),
            "volume": [10_000] * len(days),
            "turnover": [5e10] * len(days),
            "close": [3500.0] * len(days),
        }
    )


class _ScriptFakeSource:
    """脚本级分钟替身：按候选窗逐分钟造行，价格在**同一交易日内恒定**。

    恒定价把「这笔成交价来自哪一天」变成可判定的字面量，不必去猜槽位对齐 ——
    任意 5 分钟的 VWAP 都等于当日那个价。
    """

    def __init__(self, **_kwargs):
        pass

    @staticmethod
    def level(trade_date):
        return 100.0 + float(trade_date.toordinal())

    def iter_month(self, candidates, lower, upper):
        frames = []
        for candidate in candidates:
            minutes = pd.date_range(
                candidate.window_start,
                candidate.window_end,
                freq="1min",
                inclusive="left",
            )
            level = self.level(candidate.trade_date)
            frames.append(
                pd.DataFrame(
                    {
                        "trade_date": candidate.trade_date,
                        "product": candidate.product,
                        "daily_contract": candidate.daily_contract,
                        "bar_time": minutes,
                        "symbol": candidate.minute_symbol,
                        "open": level,
                        "high": level + 0.5,
                        "low": level - 0.5,
                        "close": level,
                        "volume": 10.0,
                        "amount": level * 10.0 * 10,
                    }
                )
            )
        if frames:
            yield pd.concat(frames, ignore_index=True)

    @staticmethod
    def resolve_metadata_multiplier(**_kwargs):
        return SimpleNamespace(multiplier=10)


def _script_stubs(monkeypatch, module):
    monkeypatch.setattr(module, "resolve_settings_path", lambda: Path("unused.yaml"))
    monkeypatch.setattr(module, "load_config", lambda _path: {})
    monkeypatch.setattr(module, "pg_config_from", lambda _cfg: {})
    monkeypatch.setattr(module, "load_scope_daily", lambda _pg, *, end: _daily_frame())
    monkeypatch.setattr(module, "load_session_rules", lambda _path: [_day_only_rule()])
    monkeypatch.setattr(module, "load_pricing_bases", lambda _path: {})
    monkeypatch.setattr(
        module, "pricing_basis_for", lambda _bases, _exchange: "amount_vwap"
    )
    monkeypatch.setattr(module, "require_session_coverage", lambda **_kw: None)
    monkeypatch.setattr(module, "PublicMinuteSource", _ScriptFakeSource)


def test_each_shard_prices_the_last_bar_of_its_month(monkeypatch, tmp_path):
    """月末那一根的成交价必须跨分片补上。

    这条只有**接线**才看得见：`build_panel` 自己的 `pending` 是跨月存活的，而分片
    是逐月各调一次写出来的，于是每片都从空的 `pending` 开始。全历史实测未补上的
    `fill_pending` 恰好 6,259 个 = （品种 × 分片）组合数，181 个分片无一例外。
    """
    module = _load_build_panel()
    _script_stubs(monkeypatch, module)
    out = tmp_path / "panel"

    assert module.main(
        ["--start", "2024-02", "--end", "2024-03", "--out", str(out)]
    ) == 0

    february = pd.read_parquet(out / "panel-2024-02.parquet")
    march = pd.read_parquet(out / "panel-2024-03.parquet")

    # 拿来定价的三月首日不许把 bar 漏进二月分片，否则两片重复。
    assert february["trade_date"].max() < pd.Timestamp("2024-03-01")
    assert march["trade_date"].min() >= pd.Timestamp("2024-03-01")

    tail = february.iloc[-1]
    assert bool(tail["fill_pending"]) is False
    assert tail["fill_price"] == pytest.approx(
        _ScriptFakeSource.level(march["trade_date"].min().date())
    )


def test_the_last_shard_of_the_run_keeps_its_unpriced_tail(monkeypatch, tmp_path):
    """最后一个分片之后没有下一片，那一根只能挂着 —— 不许拿别的价顶上。"""
    module = _load_build_panel()
    _script_stubs(monkeypatch, module)
    out = tmp_path / "panel"

    assert module.main(
        ["--start", "2024-02", "--end", "2024-03", "--out", str(out)]
    ) == 0

    march = pd.read_parquet(out / "panel-2024-03.parquet")
    tail = march.iloc[-1]
    assert bool(tail["fill_pending"]) is True
    assert pd.isna(tail["fill_price"])


def test_build_panel_gates_coverage_before_touching_the_minute_source(
    monkeypatch, tmp_path
):
    """闸必须挡在 PublicMinuteSource 之前。

    只断言「闸被调用了」不够 —— 闸挪到分钟源之后照样能通过那种断言，而那时第一次
    查询已经发出去了。这里让分钟源一被构造就炸，闸一被调用就截停：顺序错了，
    异常类型就不同，测试变红。
    """
    module = _load_build_panel()
    monkeypatch.setattr(module, "resolve_settings_path", lambda: Path("unused.yaml"))
    monkeypatch.setattr(module, "load_config", lambda _path: {})
    monkeypatch.setattr(module, "pg_config_from", lambda _cfg: {})
    monkeypatch.setattr(module, "load_scope_daily", lambda _pg, *, end: _daily_frame())

    def _gate(**_kwargs):
        raise _GateReached

    def _forbidden(**_kwargs):
        raise AssertionError("minute source built before the coverage gate ran")

    monkeypatch.setattr(module, "require_session_coverage", _gate)
    monkeypatch.setattr(module, "PublicMinuteSource", _forbidden)

    with pytest.raises(_GateReached):
        module.main(
            ["--start", "2024-03", "--end", "2024-03", "--out", str(tmp_path / "panel")]
        )


def test_build_panel_hands_the_gate_the_whole_requested_range(monkeypatch, tmp_path):
    """闸要拿到整个请求区间的宇宙，而不是只有当月 —— 否则又变成逐月才发现。"""
    module = _load_build_panel()
    monkeypatch.setattr(module, "resolve_settings_path", lambda: Path("unused.yaml"))
    monkeypatch.setattr(module, "load_config", lambda _path: {})
    monkeypatch.setattr(module, "pg_config_from", lambda _cfg: {})
    monkeypatch.setattr(module, "load_scope_daily", lambda _pg, *, end: _daily_frame())

    seen = {}

    def _gate(**kwargs):
        seen.update(kwargs)
        raise _GateReached

    monkeypatch.setattr(module, "require_session_coverage", _gate)
    monkeypatch.setattr(
        module, "PublicMinuteSource", lambda **_kw: pytest.fail("too early")
    )

    with pytest.raises(_GateReached):
        module.main(
            ["--start", "2024-02", "--end", "2024-03", "--out", str(tmp_path / "panel")]
        )

    assert set(seen["products_by_month"]) == {date(2024, 2, 1), date(2024, 3, 1)}
    assert seen["rules"]
    assert seen["choices"]
