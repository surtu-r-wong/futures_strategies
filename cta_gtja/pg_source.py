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

PILOT_FUNDAMENTAL_SYMBOLS = (
    "M", "RB", "CU", "AL", "TA", "PP", "MA", "BU", "RU",
)
ALLOWED_FUNDAMENTAL_SCHEMAS = frozenset({"commodity_research"})
STANDARD_FUNDAMENTAL_METRICS = (
    "spot",
    "basis_rate",
    "inventory",
    "profit",
)


def load_public_cta_data(
    *,
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
    rule_type: str = "standard",
    fundamentals_source: str = "standard",
    fundamentals_schema: str = "commodity_research",
    config_path=None,
    use_test: bool = False,
    include_financial: bool = False,
    adjustment_policy: str = "recommended",
    allow_raw_fallback: bool = False,
) -> CTADataSet:
    """Load CTA inputs from the existing ``public`` schema.

    Price source:
        ``public.continuous_contract_ohlc``.

    Fundamental sources:
        ``standard`` reads the latest complete conservative build;
        ``legacy`` reads sparse ``public`` spot and inventory tables for
        diagnostics; ``none`` skips fundamentals.
    """
    if fundamentals_source not in {"standard", "legacy", "none"}:
        raise ValueError(
            f"unsupported CTA fundamentals source: {fundamentals_source!r}"
        )
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
        if fundamentals_source == "standard":
            fundamentals, fundamental_quality, fundamental_metadata = (
                _load_standard_fundamentals(
                    conn,
                    start=start,
                    end=end,
                    symbols=symbols,
                    schema=fundamentals_schema,
                )
            )
        elif fundamentals_source == "legacy":
            fundamentals = _load_legacy_fundamentals(
                conn,
                start=start,
                end=end,
                symbols=symbols,
            )
            fundamental_quality = pd.DataFrame()
            fundamental_metadata = {
                "source": "legacy",
                "materialized_daily": False,
            }
        else:
            fundamentals = pd.DataFrame(columns=["trade_date", "symbol"])
            fundamental_quality = pd.DataFrame()
            fundamental_metadata = {"source": "none"}
    return CTADataSet(
        prices=normalize_prices(prices),
        fundamentals=normalize_fundamentals(fundamentals),
        data_quality=quality,
        fundamental_quality=fundamental_quality,
        fundamental_metadata=fundamental_metadata,
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
    if not include_financial:
        clauses.append("NOT (base_symbol = ANY(%(excluded_symbols)s))")
        params["excluded_symbols"] = sorted(FINANCIAL_FUTURES)
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


def _load_legacy_fundamentals(
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


def _load_standard_fundamentals(
    conn,
    *,
    start: date | None,
    end: date | None,
    symbols: list[str] | None,
    schema: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    if schema not in ALLOWED_FUNDAMENTAL_SCHEMAS:
        raise ValueError(
            f"unsupported fundamental schema {schema!r}; "
            f"allowed schemas: {sorted(ALLOWED_FUNDAMENTAL_SCHEMAS)}"
        )

    params: dict[str, object] = {}
    filters = [
        "b.status = 'complete'",
        "b.pit_mode = 'conservative'",
        f"""b.build_version = (
            SELECT build_version
            FROM {schema}.fundamental_build
            WHERE status = 'complete'
              AND pit_mode = 'conservative'
            ORDER BY finished_at DESC
            LIMIT 1
        )""",
        """d.available_at <=
           ((d.trade_date::timestamp + time '15:00')
            AT TIME ZONE 'Asia/Shanghai')""",
    ]
    if start is not None:
        filters.append("d.trade_date >= %(start)s")
        params["start"] = start
    if end is not None:
        filters.append("d.trade_date <= %(end)s")
        params["end"] = end
    if symbols is not None:
        filters.append("d.product_code = ANY(%(symbols)s)")
        params["symbols"] = list(symbols)
    where = "\n          AND ".join(filters)

    values_sql = f"""
        /* cta-standard-values */
        SELECT d.trade_date,
               d.product_code AS symbol,
               d.metric,
               d.value::float AS value,
               d.build_version,
               d.catalog_version,
               b.source_recorded_cutoff
        FROM {schema}.fundamental_daily AS d
        JOIN {schema}.fundamental_build AS b
          ON b.build_version = d.build_version
        WHERE {where}
        ORDER BY d.product_code, d.trade_date, d.metric
    """
    audit_sql = f"""
        /* cta-standard-audit */
        SELECT d.trade_date,
               d.product_code AS symbol,
               d.metric,
               d.build_version,
               d.catalog_version,
               d.source_observation_date,
               d.available_at,
               d.series_id,
               d.formula_id,
               d.vintage_quality,
               d.staleness_trading_days,
               d.lineage,
               d.lineage_hash
        FROM {schema}.fundamental_daily AS d
        JOIN {schema}.fundamental_build AS b
          ON b.build_version = d.build_version
        WHERE {where}
        ORDER BY d.product_code, d.trade_date, d.metric
    """

    build_sql = f"""
        /* cta-standard-build */
        SELECT b.build_version,
               b.absence_slices
        FROM {schema}.fundamental_build AS b
        WHERE b.status = 'complete'
          AND b.pit_mode = 'conservative'
          AND b.build_version = (
              SELECT build_version
              FROM {schema}.fundamental_build
              WHERE status = 'complete'
                AND pit_mode = 'conservative'
              ORDER BY finished_at DESC
              LIMIT 1
          )
    """

    values = _read_sql(values_sql, conn, params=params)
    if values.empty:
        raise ValueError("no complete conservative build")

    value_key = ["trade_date", "symbol", "metric"]
    if values.duplicated(value_key).any():
        raise ValueError("duplicate standard fundamental value rows")

    build_versions = values["build_version"].dropna().unique().tolist()
    if values["build_version"].isna().any() or len(build_versions) != 1:
        raise ValueError("expected exactly one build_version")
    catalog_versions = values["catalog_version"].dropna().unique().tolist()
    if values["catalog_version"].isna().any() or len(catalog_versions) != 1:
        raise ValueError("expected exactly one catalog_version")
    build_version = build_versions[0]
    catalog_version = catalog_versions[0]

    audit = _read_sql(audit_sql, conn, params=params)
    if audit.empty:
        raise ValueError("empty standard fundamental audit")
    if audit.duplicated(value_key).any():
        raise ValueError("duplicate standard fundamental audit rows")

    audit_build_versions = audit["build_version"].dropna().unique().tolist()
    audit_catalog_versions = audit["catalog_version"].dropna().unique().tolist()
    if audit["build_version"].isna().any() or len(audit_build_versions) != 1:
        raise ValueError("expected exactly one audit build_version")
    if audit["catalog_version"].isna().any() or len(audit_catalog_versions) != 1:
        raise ValueError("expected exactly one audit catalog_version")
    if audit_build_versions != [build_version]:
        raise ValueError("value/audit build-version mismatch")
    if audit_catalog_versions != [catalog_version]:
        raise ValueError("value/audit catalog-version mismatch")

    values = values.copy()
    values["value"] = pd.to_numeric(values["value"], errors="coerce")
    wide = (
        values[values["metric"].isin(STANDARD_FUNDAMENTAL_METRICS)]
        .pivot(
            index=["trade_date", "symbol"],
            columns="metric",
            values="value",
        )
        .reindex(columns=STANDARD_FUNDAMENTAL_METRICS)
        .reset_index()
    )
    wide.columns.name = None
    wide = wide.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    build_row = _read_sql(build_sql, conn, params={})
    if build_row.empty:
        raise ValueError("no complete conservative build row")
    if len(build_row) != 1:
        raise ValueError("expected exactly one build row")
    if build_row["build_version"].iloc[0] != build_version:
        raise ValueError("build-row/value build-version mismatch")
    absence_slices = build_row["absence_slices"].iloc[0]
    # A build that never ran with a reviewed absence file records NULL. Treat
    # that as "nothing waived" rather than "unknown": the gate then behaves
    # exactly as it did before this column existed.
    if absence_slices is None or (
        isinstance(absence_slices, float) and pd.isna(absence_slices)
    ):
        absence_slices = []

    metadata = {
        "source": "standard",
        "pit_mode": "conservative",
        "absence_slices": list(absence_slices),
        "build_version": build_version,
        "catalog_version": catalog_version,
        "source_recorded_cutoff": values["source_recorded_cutoff"].iloc[0],
        "schema": schema,
        "materialized_daily": True,
    }
    return wide, audit.reset_index(drop=True), metadata


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
            "open_raw",
            "high_raw",
            "low_raw",
            "close_raw",
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

    selected = merged["selected_adj"].to_numpy()
    for field in ("open", "high", "low", "close"):
        lineage_columns = {
            lineage: f"{field}_{lineage}"
            for lineage in ("raw", "ba", "fa")
            if f"{field}_{lineage}" in merged.columns
        }
        if not lineage_columns:
            continue
        # Vectorized equivalent of picking merged[f"{field}_{selected_adj}"] per
        # row: write each lineage's column into the rows that selected it.
        picked = pd.Series(index=merged.index, dtype="float64")
        for lineage, column in lineage_columns.items():
            mask = selected == lineage
            picked.loc[mask] = merged.loc[mask, column].to_numpy()
        out[field] = picked.to_numpy()
    return out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def _read_sql(sql: str, conn, *, params: dict[str, object]) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="pandas only supports SQLAlchemy connectable.*",
            category=UserWarning,
        )
        return pd.read_sql_query(sql, conn, params=params)
