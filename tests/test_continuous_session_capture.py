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
