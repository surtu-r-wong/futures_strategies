from contextlib import contextmanager
from datetime import date
import warnings

import pandas as pd

from cta_carry.config import CarryConfig
from cta_carry.pg_source import (
    FINANCIAL_FUTURES,
    _contract_query,
    _read_sql,
    load_public_carry_data,
)


def _valid_row():
    return {
        "trade_date": "2024-01-02",
        "contract": "rb2405.shf",
        "open": 100.0,
        "high": 104.0,
        "low": 99.0,
        "close": 102.0,
        "volume": 10.0,
        "oi": 20.0,
        "turnover": 1_000.0,
        "settle": 101.0,
    }


def test_contract_query_filters_products_and_always_excludes_financials():
    query_start = date(2022, 1, 1)
    end = date(2024, 1, 1)

    sql, params = _contract_query(
        query_start=query_start,
        end=end,
        products=["rb", "TA", " rb "],
    )

    normalized_sql = " ".join(sql.split())
    assert "FROM public.futures_daily" in normalized_sql
    assert "symbol AS contract" in normalized_sql
    assert "trade_date >= %(query_start)s" in normalized_sql
    assert "trade_date <= %(end)s" in normalized_sql
    assert "excluded_products" in normalized_sql
    assert "products" in normalized_sql
    assert "ORDER BY trade_date, symbol" in normalized_sql
    assert params["query_start"] == query_start
    assert params["end"] == end
    assert params["products"] == ["RB", "TA"]
    assert set(params["excluded_products"]) == FINANCIAL_FUTURES


def test_load_public_carry_data_uses_prewarm_and_public_schema(monkeypatch):
    from cta_carry import pg_source

    captured = {}
    connection = object()

    @contextmanager
    def fake_get_connection(pg):
        captured["pg"] = pg
        yield connection

    def fake_read_sql(sql, conn, *, params):
        captured["sql"] = sql
        captured["params"] = params
        assert conn is connection
        return pd.DataFrame([_valid_row()])

    monkeypatch.setattr(
        pg_source, "resolve_settings_path", lambda: "settings.yaml"
    )
    monkeypatch.setattr(
        pg_source,
        "load_config",
        lambda path: {"database": {"loaded_from": path}},
    )
    monkeypatch.setattr(
        pg_source,
        "pg_config_from",
        lambda cfg, use_test=False: {"name": "carry", "use_test": use_test},
    )
    monkeypatch.setattr(pg_source, "get_connection", fake_get_connection)
    monkeypatch.setattr(pg_source, "_read_sql", fake_read_sql)

    dataset = load_public_carry_data(
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        config=CarryConfig(),
        products=["rb"],
    )

    assert captured["params"]["query_start"] == date(2022, 1, 1)
    assert captured["params"]["end"] == date(2024, 2, 1)
    assert captured["params"]["products"] == ["RB"]
    assert captured["pg"]["schema"] == "public"
    assert dataset.prices["contract"].tolist() == ["RB2405.SHF"]


def test_read_sql_suppresses_only_pandas_connectable_warning(monkeypatch):
    from cta_carry import pg_source

    def fake_read_sql_query(sql, conn, params):
        warnings.warn(
            "pandas only supports SQLAlchemy connectable objects",
            UserWarning,
        )
        warnings.warn("keep this warning", UserWarning)
        return pd.DataFrame()

    monkeypatch.setattr(pg_source.pd, "read_sql_query", fake_read_sql_query)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _read_sql("SELECT 1", object(), params={})

    assert result.empty
    assert [str(item.message) for item in caught] == ["keep this warning"]
