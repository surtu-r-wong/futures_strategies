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
    excluded_products: list[str] | None = None,
    config_path=None,
    use_test: bool = False,
) -> CarryDataSet:
    """Load and normalize public-schema contract bars with prewarm history."""
    query_start = start - timedelta(days=config.prewarm_calendar_days)
    settings_path = config_path if config_path is not None else resolve_settings_path()
    settings = load_config(settings_path)
    pg = pg_config_from(settings, use_test=use_test).copy()
    pg["schema"] = "public"
    sql, params = _contract_query(
        query_start=query_start,
        end=end,
        products=products,
        excluded_products=excluded_products,
    )
    with get_connection(pg) as conn:
        frame = _read_sql(sql, conn, params=params)
    return normalize_contract_daily(frame)


def _contract_query(
    *,
    query_start: date,
    end: date,
    products: list[str] | None,
    excluded_products: list[str] | None = None,
) -> tuple[str, dict[str, object]]:
    exclusions = set(FINANCIAL_FUTURES)
    if excluded_products:
        exclusions |= {
            str(p).strip().upper() for p in excluded_products if str(p).strip()
        }
    clauses = [
        "trade_date >= %(query_start)s",
        "trade_date <= %(end)s",
        f"COALESCE(NOT ({_PRODUCT_EXPRESSION} = ANY(%(excluded_products)s)), TRUE)",
    ]
    params: dict[str, object] = {
        "query_start": query_start,
        "end": end,
        "excluded_products": sorted(exclusions),
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


def _read_sql(sql: str, conn, *, params: dict[str, object]) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="pandas only supports SQLAlchemy connectable.*",
            category=UserWarning,
        )
        return pd.read_sql_query(sql, conn, params=params)


def load_public_product_history_starts(
    *, config_path=None, use_test: bool = False
) -> pd.DataFrame:
    """Return product and first available daily date from public.futures_daily."""
    settings_path = config_path if config_path is not None else resolve_settings_path()
    settings = load_config(settings_path)
    pg = pg_config_from(settings, use_test=use_test).copy()
    pg["schema"] = "public"
    sql, params = _product_history_starts_query()
    with get_connection(pg) as conn:
        frame = _read_sql(sql, conn, params=params)

    required = {"product", "first_trade_date"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"product history starts missing columns: {missing}")
    normalized = frame.loc[:, ["product", "first_trade_date"]].copy()
    normalized["product"] = (
        normalized["product"].astype("string").str.strip().str.upper()
    )
    normalized["first_trade_date"] = pd.to_datetime(
        normalized["first_trade_date"], errors="coerce"
    ).dt.date
    if normalized["product"].isna().any() or normalized["product"].eq("").any():
        raise ValueError("product history starts contain an empty product")
    if normalized["first_trade_date"].isna().any():
        raise ValueError("product history starts contain an invalid first_trade_date")
    if normalized.duplicated("product").any():
        raise ValueError("product history starts contain duplicate products")
    return normalized.sort_values("product", kind="mergesort").reset_index(drop=True)


def _product_history_starts_query() -> tuple[str, dict[str, object]]:
    params: dict[str, object] = {
        "excluded_products": sorted(FINANCIAL_FUTURES),
    }
    sql = f"""
        SELECT {_PRODUCT_EXPRESSION} AS product,
               MIN(trade_date) AS first_trade_date
        FROM public.futures_daily
        WHERE COALESCE(NOT ({_PRODUCT_EXPRESSION} =
              ANY(%(excluded_products)s)), TRUE)
        GROUP BY {_PRODUCT_EXPRESSION}
        ORDER BY product
    """
    return sql, params


def load_public_exchange_coverage(
    *, config_path=None, since: date, use_test: bool = False
) -> dict[str, date]:
    """max(trade_date) per exchange suffix over rows on or after `since`."""
    settings_path = config_path if config_path is not None else resolve_settings_path()
    settings = load_config(settings_path)
    pg = pg_config_from(settings, use_test=use_test).copy()
    pg["schema"] = "public"
    sql = """
        SELECT split_part(symbol, '.', 2) AS exchange, MAX(trade_date) AS max_trade_date
        FROM public.futures_daily
        WHERE trade_date >= %(since)s
        GROUP BY 1
    """
    with get_connection(pg) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout = '60s'")
        cur.execute(sql, {"since": since})
        rows = cur.fetchall()
    return {str(exchange): max_trade_date for exchange, max_trade_date in rows}
