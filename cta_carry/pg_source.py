"""Read-only PostgreSQL source for Carry contract bars."""
from __future__ import annotations

from datetime import date, timedelta
import warnings

import pandas as pd

from common.config import load_config, resolve_settings_path
from common.db import get_connection, pg_config_from

from .config import CarryConfig
from .data import CarryDataSet, normalize_contract_daily


FINANCIAL_FUTURES = frozenset({"IF", "IC", "IH", "IM", "T", "TF", "TL", "TS"})
_PRODUCT_EXPRESSION = "UPPER(substring(symbol from '^[A-Za-z]+'))"


def load_public_carry_data(
    *,
    start: date,
    end: date,
    config: CarryConfig,
    products: list[str] | None = None,
    config_path=None,
    use_test: bool = False,
) -> CarryDataSet:
    """Load and normalize public-schema contract bars with prewarm history."""
    query_start = start - timedelta(days=config.prewarm_calendar_days)
    settings_path = (
        config_path if config_path is not None else resolve_settings_path()
    )
    settings = load_config(settings_path)
    pg = pg_config_from(settings, use_test=use_test).copy()
    pg["schema"] = "public"
    sql, params = _contract_query(
        query_start=query_start,
        end=end,
        products=products,
    )
    with get_connection(pg) as conn:
        frame = _read_sql(sql, conn, params=params)
    return normalize_contract_daily(frame)


def _contract_query(
    *,
    query_start: date,
    end: date,
    products: list[str] | None,
) -> tuple[str, dict[str, object]]:
    clauses = [
        "trade_date >= %(query_start)s",
        "trade_date <= %(end)s",
        f"COALESCE(NOT ({_PRODUCT_EXPRESSION} = "
        "ANY(%(excluded_products)s)), TRUE)",
    ]
    params: dict[str, object] = {
        "query_start": query_start,
        "end": end,
        "excluded_products": sorted(FINANCIAL_FUTURES),
    }
    normalized_products = (
        sorted({str(p).strip().upper() for p in products if str(p).strip()})
        if products
        else []
    )
    if normalized_products:
        clauses.append(f"{_PRODUCT_EXPRESSION} = ANY(%(products)s)")
        params["products"] = normalized_products

    where = " AND ".join(clauses)
    sql = f"""
        SELECT
            trade_date,
            symbol AS contract,
            open::float AS open,
            high::float AS high,
            low::float AS low,
            close::float AS close,
            volume::float AS volume,
            oi::float AS oi,
            turnover::float AS turnover,
            settle::float AS settle
        FROM public.futures_daily
        WHERE {where}
        ORDER BY trade_date, symbol
    """
    return sql, params


def _read_sql(
    sql: str, conn, *, params: dict[str, object]
) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="pandas only supports SQLAlchemy connectable.*",
            category=UserWarning,
        )
        return pd.read_sql_query(sql, conn, params=params)
