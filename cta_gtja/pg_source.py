"""PostgreSQL public-schema source for CTA futures data."""
from __future__ import annotations

from datetime import date
import warnings

import pandas as pd

from cta_gtja.data import CTADataSet, normalize_fundamentals, normalize_prices
from cta_gtja.data_quality import build_adjustment_audit, summarize_adjustment_quality
from common.config import load_config, resolve_settings_path
from common.db import get_connection, pg_config_from

FINANCIAL_FUTURES = frozenset({"IF", "IC", "IH", "IM", "T", "TF", "TL", "TS"})


def load_public_cta_data(
    *,
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
    rule_type: str = "standard",
    config_path=None,
    use_test: bool = False,
    include_financial: bool = False,
    adjustment_policy: str = "recommended",
    allow_raw_fallback: bool = False,
) -> CTADataSet:
    """Load CTA inputs from the existing ``public`` schema.

    Price source:
        ``public.continuous_contract_ohlc``.

    Fundamental source:
        ``public.spot_prices`` and ``public.inventory``.  Current database
        coverage is sparse; missing symbols stay NaN and are ignored by the
        relevant factors.
    """
    cfg = load_config(config_path or resolve_settings_path())
    pg = pg_config_from(cfg, use_test=use_test).copy()
    pg["schema"] = "public"
    with get_connection(pg) as conn:
        prices, quality = _load_prices(
            conn,
            start=start,
            end=end,
            symbols=symbols,
            rule_type=rule_type,
            include_financial=include_financial,
            adjustment_policy=adjustment_policy,
            allow_raw_fallback=allow_raw_fallback,
        )
        fundamentals = _load_fundamentals(
            conn,
            start=start,
            end=end,
            symbols=symbols,
        )
    return CTADataSet(
        prices=normalize_prices(prices),
        fundamentals=normalize_fundamentals(fundamentals),
        data_quality=quality,
    )


def _load_prices(
    conn,
    *,
    start: date | None,
    end: date | None,
    symbols: list[str] | None,
    rule_type: str,
    include_financial: bool,
    adjustment_policy: str,
    allow_raw_fallback: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    clauses = ["rule_type = %(rule_type)s"]
    params: dict[str, object] = {"rule_type": rule_type}
    if start is not None:
        clauses.append("trade_date >= %(start)s")
        params["start"] = start
    if end is not None:
        clauses.append("trade_date <= %(end)s")
        params["end"] = end
    if symbols:
        clauses.append("base_symbol = ANY(%(symbols)s)")
        params["symbols"] = list(symbols)
    elif not include_financial:
        clauses.append("NOT (base_symbol = ANY(%(excluded_symbols)s))")
        params["excluded_symbols"] = list(FINANCIAL_FUTURES)
    where = " AND ".join(clauses)
    sql = f"""
        SELECT
            trade_date,
            base_symbol AS symbol,
            contract_used AS contract,
            open_raw, open_ba, open_fa,
            high_raw, high_ba, high_fa,
            low_raw, low_ba, low_fa,
            close_raw, close_ba, close_fa,
            volume,
            oi AS open_interest,
            turnover,
            daily_return,
            pure_price_return,
            roll_contribution
        FROM public.continuous_contract_ohlc
        WHERE {where}
        ORDER BY base_symbol, trade_date
    """
    raw = _read_sql(sql, conn, params=params)
    if raw.empty:
        empty_quality = pd.DataFrame(columns=["base_symbol", "selected_adj", "included"])
        empty_prices = pd.DataFrame(columns=["trade_date", "symbol", "open", "close"])
        return empty_prices, empty_quality
    if adjustment_policy != "recommended":
        raise ValueError(f"unsupported CTA adjustment_policy: {adjustment_policy!r}")
    quality_report = summarize_adjustment_quality(raw.rename(columns={"symbol": "base_symbol"}))
    quality = build_adjustment_audit(
        quality_report,
        allow_raw_fallback=allow_raw_fallback,
    )
    prices = _apply_adjustment_policy(raw, quality)
    if prices.empty:
        raise ValueError("CTA price reader excluded all symbols under the adjustment policy")
    return prices, quality


def _load_fundamentals(
    conn,
    *,
    start: date | None,
    end: date | None,
    symbols: list[str] | None,
) -> pd.DataFrame:
    params: dict[str, object] = {}
    clauses = []
    if start is not None:
        clauses.append("trade_date >= %(start)s")
        params["start"] = start
    if end is not None:
        clauses.append("trade_date <= %(end)s")
        params["end"] = end
    if symbols:
        clauses.append("product_code = ANY(%(symbols)s)")
        params["symbols"] = list(symbols)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    spot = _read_sql(
        f"""
        SELECT trade_date, product_code AS symbol, AVG(spot_price)::float AS spot
        FROM public.spot_prices
        {where}
        GROUP BY trade_date, product_code
        ORDER BY product_code, trade_date
        """,
        conn,
        params=params,
    )
    inv = _read_sql(
        f"""
        SELECT trade_date, product_code AS symbol, AVG(inventory_value)::float AS inventory
        FROM public.inventory
        {where}
        GROUP BY trade_date, product_code
        ORDER BY product_code, trade_date
        """,
        conn,
        params=params,
    )
    if spot.empty and inv.empty:
        return pd.DataFrame(columns=["trade_date", "symbol", "spot", "inventory"])
    if spot.empty:
        merged = inv
    elif inv.empty:
        merged = spot
    else:
        merged = pd.merge(spot, inv, on=["trade_date", "symbol"], how="outer")
    return merged.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def _apply_adjustment_policy(prices: pd.DataFrame, quality: pd.DataFrame) -> pd.DataFrame:
    """Select open/close from each symbol's audited adjustment lineage."""
    if prices.empty:
        return prices.copy()
    decisions = quality[quality["included"]].copy()
    if decisions.empty:
        return pd.DataFrame(columns=["trade_date", "symbol", "open", "close"])

    merged = prices.merge(
        decisions[["base_symbol", "selected_adj"]],
        left_on="symbol",
        right_on="base_symbol",
        how="inner",
    )
    if merged.empty:
        return pd.DataFrame(columns=["trade_date", "symbol", "open", "close"])

    base_cols = [
        c for c in [
            "trade_date",
            "symbol",
            "contract",
            "volume",
            "open_interest",
            "turnover",
            "daily_return",
            "pure_price_return",
            "roll_contribution",
            "selected_adj",
        ]
        if c in merged.columns
    ]
    out = merged[base_cols].copy()
    out = out.rename(columns={"selected_adj": "adjustment_lineage"})

    for field in ("open", "high", "low", "close"):
        if not any(f"{field}_{lineage}" in merged.columns for lineage in ("raw", "ba", "fa")):
            continue
        values = []
        for _, row in merged.iterrows():
            values.append(row[f"{field}_{row['selected_adj']}"])
        out[field] = values
    return out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def _read_sql(sql: str, conn, *, params: dict[str, object]) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="pandas only supports SQLAlchemy connectable.*",
            category=UserWarning,
        )
        return pd.read_sql_query(sql, conn, params=params)
