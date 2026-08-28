"""连续策略的时段采集驱动。

补采存在的唯一理由，是让时段资产覆盖**面板会索取的那些品种日**。所以这里最要紧的
一条测试不是「驱动能跑」，而是「驱动要覆盖的集合 == `build_contexts` 实际索取的
集合」—— 两者一旦分叉，补采就会漏，全历史面板又会在某个月崩掉。
"""

from datetime import date

import pandas as pd

from common.minute.sessions import SessionRule
from cta_continuous.panel import build_contexts, context_choices_for_month
from cta_continuous.scope import panel_scope
from scripts.continuous.capture_sessions import capture_keys

START = date(2024, 3, 1)
END = date(2024, 4, 1)


def _daily():
    """RB 成交额远超 50 亿门槛；CU 远低于门槛，只应出现在行情里、不该进宇宙。"""
    days = list(pd.bdate_range("2023-09-01", "2024-04-30").date)
    rows = []
    for day in days:
        rows.append(
            {
                "symbol": "RB2405.SHF", "trade_date": day, "oi": 10_000,
                "volume": 10_000, "turnover": 5e10, "close": 3500.0,
            }
        )
        rows.append(
            {
                "symbol": "CU2405.SHF", "trade_date": day, "oi": 9_000,
                "volume": 9_000, "turnover": 1e8, "close": 70000.0,
            }
        )
    return pd.DataFrame(rows)


def _day_only(product):
    return SessionRule.day_only("SHFE", product, version="commodity-v1")


def test_capture_covers_exactly_what_build_contexts_asks_for():
    """对着真正的消费者 `build_contexts` 比对，而不是只跟共用 helper 自我印证。

    CU 在行情里但成交额远低于门槛。驱动若漏掉宇宙过滤，就会多出 CU 的键；
    若用了不同的预热窗口或不同的主力口径，键就会对不上。
    """
    stats = _daily()
    scope = panel_scope(stats, start=START, end=END)

    demanded = set()
    for month, products in scope.products_by_month.items():
        eligible = tuple(
            choice for choice in scope.choices if choice.product in set(products)
        )
        if not eligible:
            continue
        contexts = build_contexts(
            context_choices_for_month(eligible, month_start=month),
            rules=[_day_only("RB"), _day_only("CU")],
        )
        demanded |= {
            (context.candidate.exchange, product, trade_date)
            for (trade_date, product), context in contexts.items()
        }

    assert demanded, "夹具必须真的产出上下文，否则这条测试是空断言"
    assert capture_keys(stats, start=START, end=END) == demanded
    assert {key[1] for key in demanded} == {"RB"}


import pytest  # noqa: E402

from cta_carry.config import CarryConfig  # noqa: E402
from scripts.carry.capture_minute_sessions import SessionCaptureError  # noqa: E402
from scripts.continuous.capture_sessions import audit_builder_for  # noqa: E402

_CAPTURE_DAYS = [date(2024, 3, day) for day in (4, 5, 6)]


def _capture_prices():
    return pd.DataFrame(
        [
            {
                "trade_date": day, "product": "RB", "contract": "RB2405.SHF",
                "oi": 10_000, "volume": 10_000,
            }
            for day in _CAPTURE_DAYS
        ]
    )


def _build(keys, *, start, end):
    return audit_builder_for(frozenset(keys))(
        _capture_prices(),
        history_starts=None,
        history_exceptions=(),
        start=start,
        end=end,
        config=CarryConfig(),
    )


def test_builder_uses_the_continuous_pool_verbatim():
    key = ("SHFE", "RB", _CAPTURE_DAYS[1])

    audit = _build([key], start=_CAPTURE_DAYS[1], end=_CAPTURE_DAYS[1])

    assert audit.key_sets.in_pool_keys == frozenset({key})
    assert audit.history_status_by_key == {
        ("SHFE", "RB", _CAPTURE_DAYS[1]): "lookback_complete"
    }


def test_builder_refuses_when_the_panel_needs_a_day_the_capture_frame_lacks():
    """少采就是把同一个覆盖缺口原样留到下一次全历史跑 —— 必须当场炸。"""
    missing = ("SHFE", "CU", _CAPTURE_DAYS[1])

    with pytest.raises(SessionCaptureError, match="continuous_pool_outside_normalized"):
        _build(
            [("SHFE", "RB", _CAPTURE_DAYS[1]), missing],
            start=_CAPTURE_DAYS[1],
            end=_CAPTURE_DAYS[1],
        )


def test_builder_refuses_a_start_too_early_for_the_turnover_lookback():
    """宇宙口径回看半年；日线起点不够早，首月宇宙就是错的。"""
    with pytest.raises(SessionCaptureError, match="continuous_universe_prewarm"):
        _build([], start=date(2010, 3, 1), end=date(2010, 3, 1))


from common.minute.sessions import SessionClockError  # noqa: E402
from scripts.continuous.capture_sessions import (  # noqa: E402
    continuous_capture_coverage,
    month_start,
)


def test_continuous_coverage_accepts_a_panel_starting_at_the_asset_first_day():
    """Carry 的 730 天预热在这里会拒掉它；连续面板本来就从资产首日开始。"""
    first = date(2011, 1, 4)

    assert (
        continuous_capture_coverage(
            capture_start=first, backtest_start=first, prewarm_calendar_days=730
        )
        == first
    )


def test_continuous_coverage_refuses_an_asset_that_starts_after_the_panel():
    """分段采集若从 2018 起，就盖不住 2011 起的面板 —— 这条必须拦。"""
    with pytest.raises(SessionClockError, match="session_asset_starts_after_panel"):
        continuous_capture_coverage(
            capture_start=date(2018, 1, 2),
            backtest_start=date(2011, 1, 4),
            prewarm_calendar_days=730,
        )


def test_month_start_folds_a_capture_date_onto_the_panel_month():
    assert month_start(date(2011, 1, 4)) == date(2011, 1, 1)


# --- 普查：二分定位缺分钟数据的品种日 ---------------------------------------

from types import SimpleNamespace  # noqa: E402

from common.minute.bars import MinuteDataError  # noqa: E402
from scripts.continuous.capture_sessions import (  # noqa: E402
    survey_boundaries,
    write_blocked_manifest,
)


def _candidate(day, product="RB"):
    return SimpleNamespace(
        candidate=SimpleNamespace(
            exchange="SHFE",
            product=product,
            trade_date=day,
            daily_contract=f"{product}2405.SHF",
        )
    )


def _capture_missing(broken_days):
    """撞上任何一个坏候选就抛 —— 与真实 `iter_session_boundaries` 的姿态一致。"""
    calls = []

    def capture(batch):
        calls.append(len(batch))
        bad = [
            item for item in batch if item.candidate.trade_date in broken_days
        ]
        if bad:
            raise MinuteDataError(
                check="session_representative_missing_minutes",
                reason="session representative has no minute observations",
                trade_date=bad[0].candidate.trade_date,
                product=bad[0].candidate.product,
                contract=bad[0].candidate.daily_contract,
            )
        return pd.DataFrame(
            [{"trade_date": item.candidate.trade_date} for item in batch]
        )

    return capture, calls


def test_survey_isolates_every_product_day_with_no_minute_bars():
    """权威采集撞上第一个就抛，只能知道一个；普查要一次拿到完整清单。"""
    days = [date(2024, 3, 1) + pd.Timedelta(days=n).to_pytimedelta() for n in range(8)]
    items = [_candidate(day) for day in days]
    capture, calls = _capture_missing({days[2], days[5]})

    frame, blocked = survey_boundaries(items, capture=capture)

    assert {(item.product, item.trade_date) for item in blocked} == {
        ("RB", days[2]),
        ("RB", days[5]),
    }
    assert len(frame) == 6, "其余 6 个候选的观测必须照常拿到"
    assert all(
        item.check == "session_representative_missing_minutes" for item in blocked
    )
    # 二分而不是逐个查：8 个候选、2 个坏的，远少于 8 次单点查询。
    assert sum(1 for size in calls if size == 1) < 8


def test_survey_makes_no_extra_queries_when_nothing_is_missing():
    items = [_candidate(date(2024, 3, 1))]
    capture, calls = _capture_missing(frozenset())

    frame, blocked = survey_boundaries(items, capture=capture)

    assert blocked == ()
    assert len(frame) == 1
    assert calls == [1]


def test_blocked_manifest_lists_every_row(tmp_path):
    items = [_candidate(date(2024, 3, 4))]
    capture, _ = _capture_missing({date(2024, 3, 4)})
    _frame, blocked = survey_boundaries(items, capture=capture)
    path = tmp_path / "blocked.csv"

    write_blocked_manifest(blocked, path)

    rows = path.read_text(encoding="utf-8").strip().splitlines()
    assert rows[0] == "exchange,product,trade_date,daily_contract,check,reason"
    assert rows[1].startswith("SHFE,RB,2024-03-04,RB2405.SHF,")
    assert len(rows) == 2


# --- 边界观测缓存 -----------------------------------------------------------

from scripts.continuous.capture_sessions import (  # noqa: E402
    read_boundary_cache,
    write_boundary_cache,
)

_KEYS_A = frozenset({("SHFE", "RB", date(2024, 3, 4))})
_KEYS_B = frozenset({("SHFE", "RB", date(2024, 3, 5))})


def _observations():
    """真实边界帧的这两列就是 python ``date``，夹具必须照实。"""
    return pd.DataFrame(
        [
            {
                "trade_date": date(2024, 3, 4),
                "previous_trade_date": date(2024, 3, 1),
                "product": "RB",
                "n": 225,
            }
        ]
    )


def test_boundary_cache_round_trips(tmp_path):
    path = tmp_path / "boundaries.parquet"
    write_boundary_cache(_observations(), path, keys=_KEYS_A)

    restored = read_boundary_cache(path, keys=_KEYS_A)

    assert restored["n"].tolist() == [225]
    # 日期列必须原样是 python date 回来，不能变成 Timestamp —— 下游按 date 比键。
    assert restored["trade_date"].tolist() == [date(2024, 3, 4)]
    assert restored["previous_trade_date"].tolist() == [date(2024, 3, 1)]


def test_boundary_cache_refuses_a_different_key_set(tmp_path):
    """键集变了缓存就不是这次要的观测 —— 拒绝，不静默沿用。"""
    path = tmp_path / "boundaries.parquet"
    write_boundary_cache(_observations(), path, keys=_KEYS_A)

    with pytest.raises(SessionCaptureError, match="boundary_cache_stale"):
        read_boundary_cache(path, keys=_KEYS_B)


def test_boundary_cache_absent_means_capture_fresh(tmp_path):
    assert read_boundary_cache(tmp_path / "nope.parquet", keys=_KEYS_A) is None


def test_boundary_cache_refuses_a_parquet_with_no_digest(tmp_path):
    """只有数据没有摘要，就无法确认它属于这次采集 —— 同样拒绝。"""
    path = tmp_path / "boundaries.parquet"
    write_boundary_cache(_observations(), path, keys=_KEYS_A)
    path.with_name(path.name + ".digest").unlink()

    with pytest.raises(SessionCaptureError, match="boundary_cache_stale"):
        read_boundary_cache(path, keys=_KEYS_A)


def test_survey_never_bisects_across_month_boundaries():
    """二分必须限制在月内。

    `capture_session_boundaries` 本来就按月分批查询；若在全量候选上二分，一个坏候选
    会把整份列表对半重查，每层重读约 n 行、深度约 log2(n) —— 单个失败就是十几倍的
    基础成本。月内二分把重查代价限制在那一个月。
    """
    january = [_candidate(date(2024, 1, day)) for day in (2, 3, 4, 5)]
    february = [_candidate(date(2024, 2, day)) for day in (1, 2, 3, 4)]
    capture, _calls = _capture_missing({date(2024, 1, 3)})
    seen_batches = []

    def recording_capture(batch):
        seen_batches.append(
            {(item.candidate.trade_date.year, item.candidate.trade_date.month)
             for item in batch}
        )
        return capture(batch)

    _frame, blocked = survey_boundaries(january + february, capture=recording_capture)

    assert [item.trade_date for item in blocked] == [date(2024, 1, 3)]
    assert all(len(months) == 1 for months in seen_batches), (
        "任何一批都不能跨月，否则二分会把别的月份也拖进重查"
    )
    # 二月完好，必须一次查完，不能因为一月失败而被拆
    assert seen_batches.count({(2024, 2)}) == 1


def test_survey_reports_progress_per_month():
    """3 小时的长跑没有心跳就无法判活体（[[long-jobs-need-setsid]] 的教训）。"""
    items = [_candidate(date(2024, 1, 2)), _candidate(date(2024, 2, 1))]
    capture, _ = _capture_missing(frozenset())
    beats = []

    survey_boundaries(items, capture=capture, on_month=lambda *args: beats.append(args))

    assert [beat[0] for beat in beats] == [(2024, 1), (2024, 2)]
