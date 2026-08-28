"""The commodity session capture's observation cache."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from scripts.commodity.capture_sessions import (
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
