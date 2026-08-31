"""The commodity session capture's observation cache."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from scripts.carry.capture_minute_sessions import classify_session_boundary
from scripts.commodity.capture_sessions import (
    boundary_cache_digest,
    read_boundary_cache,
    write_boundary_cache,
)


def _observations() -> pd.DataFrame:
    """Boundary observations as the capture produces them: object columns whose
    empties are ``None`` (a product-day with no night session)."""
    return pd.DataFrame(
        {
            "trade_date": [date(2024, 1, 2), date(2024, 1, 3)],
            "previous_trade_date": [date(2023, 12, 29), date(2024, 1, 2)],
            "product": ["RB", "AP"],
            "night_first": [
                pd.Timestamp("2024-01-01 21:00", tz="Asia/Shanghai"),
                None,
            ],
            "night_traded_first": [
                pd.Timestamp("2024-01-01 21:01", tz="Asia/Shanghai"),
                None,
            ],
            "day_1_first": [
                pd.Timestamp("2024-01-02 09:00", tz="Asia/Shanghai"),
                pd.Timestamp("2024-01-03 09:00", tz="Asia/Shanghai"),
            ],
        }
    ).astype(
        {
            "night_first": "object",
            "night_traded_first": "object",
            "day_1_first": "object",
        }
    )


def test_boundary_cache_round_trips_nullable_aware_columns(tmp_path):
    """空值混着 tz-aware 时刻的 object 列，fastparquet 推不出类型、直接拒写。

    没有夜盘的品种日就长这样，所以这不是边角情况；而按列名抄一份清单会漏
    （`night_traded_first` 不在 `BOUNDARY_COLUMNS` 里），只能按 dtype 判。
    """
    frame = _observations()
    path = tmp_path / "boundaries.parquet"

    write_boundary_cache(frame, path, keys=frozenset())
    restored = read_boundary_cache(path, keys=frozenset())

    assert restored["night_first"].iloc[0] == frame["night_first"].iloc[0]
    assert restored["night_traded_first"].iloc[0] == frame["night_traded_first"].iloc[0]
    assert restored["day_1_first"].tolist() == frame["day_1_first"].tolist()
    assert restored["trade_date"].tolist() == frame["trade_date"].tolist()
    # 分类器按 None 判空；还原成 NaT 会让复用缓存那一跑与直接观测那一跑不同。
    assert restored["night_first"].iloc[1] is None
    assert restored["night_traded_first"].iloc[1] is None


def test_boundary_cache_refuses_a_stale_key_set(tmp_path):
    path = tmp_path / "boundaries.parquet"
    write_boundary_cache(_observations(), path, keys=frozenset({("SHFE", "RB", date(2024, 1, 2))}))

    with pytest.raises(Exception, match="boundary_cache_stale"):
        read_boundary_cache(path, keys=frozenset({("SHFE", "AP", date(2024, 1, 2))}))


def test_a_missing_cache_is_not_an_error(tmp_path):
    assert read_boundary_cache(tmp_path / "absent.parquet", keys=frozenset()) is None


def _auction_night_observation() -> pd.DataFrame:
    """2019-12-26 那一晚的形状：开盘推迟到 22:30，22:29 那根是集合竞价的成交。"""
    previous = pd.Timestamp("2019-12-25", tz="Asia/Shanghai")

    def day(hour, minute):
        return [
            pd.Timestamp(f"2019-12-26 {hour:02d}:{minute:02d}", tz="Asia/Shanghai")
        ] * 2

    # 第二行是同一天里一个没有夜盘的品种 —— 采集产出里这样的行占三成，正是它们
    # 把标志列拖成可空列。
    return pd.DataFrame(
        {
            "trade_date": [date(2019, 12, 26)] * 2,
            "previous_trade_date": [date(2019, 12, 25)] * 2,
            "exchange": ["SHFE", "CZCE"],
            "product": ["RB", "AP"],
            "daily_contract": ["RB2005.SHF", "AP005.CZC"],
            "night_first": [previous + pd.Timedelta(hours=21), None],
            "night_last": [previous + pd.Timedelta(hours=22, minutes=59), None],
            "night_traded_first": [previous + pd.Timedelta(hours=22, minutes=29), None],
            "night_traded_second": [
                previous + pd.Timedelta(hours=22, minutes=30),
                None,
            ],
            "night_traded_first_flat": [True, None],
            "day_1_first": day(9, 0),
            "day_1_last": day(10, 14),
            "day_2_first": day(10, 30),
            "day_2_last": day(11, 29),
            "day_3_first": day(13, 30),
            "day_3_last": day(14, 59),
        }
    ).astype({column: "object" for column in _AWARE_COLUMNS})


_AWARE_COLUMNS = (
    "night_first",
    "night_last",
    "night_traded_first",
    "night_traded_second",
    "day_1_first",
    "day_1_last",
    "day_2_first",
    "day_2_last",
    "day_3_first",
    "day_3_last",
)


def test_a_cached_auction_bar_is_still_attributed_to_the_session_it_opened(tmp_path):
    """归位规则要求 ``night_traded_first_flat is True``，所以缓存必须把它作为**布尔**
    还原回来。parquet 把它存成 float，取回来是 ``1.0`` —— 归位静默失效，22:29 变成
    非法的夜盘标签，一晚全市场的观测集体判歧义。"""
    frame = _auction_night_observation()
    path = tmp_path / "boundaries.parquet"

    write_boundary_cache(frame, path, keys=frozenset())
    restored = read_boundary_cache(path, keys=frozenset())

    live = classify_session_boundary(frame.to_dict("records")[0])
    cached = classify_session_boundary(restored.to_dict("records")[0])
    assert (live.night_start, live.night_end) == ("22:30", "23:00")
    assert (cached.night_start, cached.night_end) == (live.night_start, live.night_end)
    assert cached.note == live.note


def test_a_cache_written_before_the_dtype_fix_is_refused(tmp_path):
    """旧写法把可空布尔列存成 float，读回来 ``is True`` 判 false —— 归位规则不报错、
    只是不再生效。缓存这层的全部价值就是「复用与直接观测行为相同」，所以认不出的
    格式必须当场拒绝，而不是尽力还原。"""
    frame = _auction_night_observation()
    path = tmp_path / "boundaries.parquet"
    write_boundary_cache(frame, path, keys=frozenset())
    legacy = path.with_name(path.name + ".digest")
    legacy.write_text(boundary_cache_digest(frozenset()), encoding="utf-8")

    with pytest.raises(Exception, match="boundary_cache_format"):
        read_boundary_cache(path, keys=frozenset())
