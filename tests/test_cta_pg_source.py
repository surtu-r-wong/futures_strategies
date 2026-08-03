from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from cta_gtja import pg_source
from cta_gtja.pg_source import _apply_adjustment_policy, _load_prices


def _prices():
    return pd.DataFrame([
        {
            "trade_date": "2026-01-02",
            "symbol": "M",
            "contract": "M2601",
            "open_raw": 10.0,
            "open_ba": 100.0,
            "open_fa": -1.0,
            "high_raw": 12.0,
            "high_ba": 102.0,
            "high_fa": -0.5,
            "low_raw": 9.0,
            "low_ba": 99.0,
            "low_fa": -1.5,
            "close_raw": 11.0,
            "close_ba": 101.0,
            "close_fa": -2.0,
            "volume": 1000,
            "open_interest": 200,
        },
        {
            "trade_date": "2026-01-02",
            "symbol": "RU",
            "contract": "RU2601",
            "open_raw": 20.0,
            "open_ba": -3.0,
            "open_fa": -4.0,
            "high_raw": 22.0,
            "high_ba": -2.0,
            "high_fa": -3.0,
            "low_raw": 19.0,
            "low_ba": -4.0,
            "low_fa": -5.0,
            "close_raw": 21.0,
            "close_ba": -5.0,
            "close_fa": -6.0,
            "volume": 2000,
            "open_interest": 300,
        },
    ])


def test_explicit_symbols_still_exclude_financial_futures(monkeypatch):
    captured = {}

    def fake_read_sql(sql, conn, *, params):
        captured["sql"] = sql
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr("cta_gtja.pg_source._read_sql", fake_read_sql)

    _load_prices(
        object(),
        start=None,
        end=None,
        symbols=["IF", "CU"],
        rule_type="standard",
        include_financial=False,
        adjustment_policy="recommended",
        allow_raw_fallback=False,
    )

    assert "base_symbol = ANY(%(symbols)s)" in captured["sql"]
    assert "NOT (base_symbol = ANY(%(excluded_symbols)s))" in captured["sql"]
    assert captured["params"]["symbols"] == ["IF", "CU"]
    assert captured["params"]["excluded_symbols"] == [
        "IC",
        "IF",
        "IH",
        "IM",
        "T",
        "TF",
        "TL",
        "TS",
    ]


def test_apply_adjustment_policy_uses_selected_lineage_and_excludes_default_raw():
    audit = pd.DataFrame([
        {
            "base_symbol": "M",
            "selected_adj": "ba",
            "included": True,
            "status": "fa_corrupt",
            "recommended_adj": "ba",
            "raw_fallback": False,
            "exclusion_reason": "",
        },
        {
            "base_symbol": "RU",
            "selected_adj": "",
            "included": False,
            "status": "both_corrupt",
            "recommended_adj": "raw",
            "raw_fallback": False,
            "exclusion_reason": "both_adjusted_lineages_corrupt",
        },
    ])

    out = _apply_adjustment_policy(_prices(), audit)

    assert out["symbol"].tolist() == ["M"]
    assert out.loc[0, "open"] == pytest.approx(100.0)
    assert out.loc[0, "close"] == pytest.approx(101.0)
    assert out.loc[0, "adjustment_lineage"] == "ba"


def test_apply_adjustment_policy_preserves_raw_ohlc_for_economic_calculations():
    audit = pd.DataFrame(
        [
            {
                "base_symbol": "M",
                "selected_adj": "ba",
                "included": True,
            }
        ]
    )

    out = _apply_adjustment_policy(_prices(), audit)

    assert out.loc[0, ["open", "high", "low", "close"]].tolist() == [
        100.0, 102.0, 99.0, 101.0,
    ]
    assert out.loc[
        0, ["open_raw", "high_raw", "low_raw", "close_raw"]
    ].tolist() == [10.0, 12.0, 9.0, 11.0]


def test_apply_adjustment_policy_allows_explicit_raw_rows():
    audit = pd.DataFrame([
        {
            "base_symbol": "RU",
            "selected_adj": "raw",
            "included": True,
            "status": "both_corrupt",
            "recommended_adj": "raw",
            "raw_fallback": True,
            "exclusion_reason": "",
        },
    ])

    out = _apply_adjustment_policy(_prices(), audit)

    assert out["symbol"].tolist() == ["RU"]
    assert out.loc[0, "open"] == pytest.approx(20.0)
    assert out.loc[0, "close"] == pytest.approx(21.0)
    assert out.loc[0, "adjustment_lineage"] == "raw"


def _standard_values(
    *,
    build_version: str = "build-c-1",
    catalog_version: str = "v1",
) -> pd.DataFrame:
    metric_values = {
        "spot": 100.0,
        "basis_rate": 0.01,
        "inventory": 80.0,
        "profit": 12.0,
    }
    rows = []
    for symbol, offset in [("M", 0.0), ("CU", 10.0)]:
        for metric, value in metric_values.items():
            rows.append(
                {
                    "trade_date": date(2026, 1, 2),
                    "symbol": symbol,
                    "metric": metric,
                    "value": value + offset,
                    "build_version": build_version,
                    "catalog_version": catalog_version,
                    "source_recorded_cutoff": pd.Timestamp("2026-01-01"),
                }
            )
    return pd.DataFrame(rows)


def _standard_audit(
    values: pd.DataFrame,
    *,
    build_version: str | None = None,
    catalog_version: str | None = None,
) -> pd.DataFrame:
    audit = values[
        ["trade_date", "symbol", "metric", "build_version", "catalog_version"]
    ].copy()
    if build_version is not None:
        audit["build_version"] = build_version
    if catalog_version is not None:
        audit["catalog_version"] = catalog_version
    audit["source_observation_date"] = date(2026, 1, 1)
    audit["available_at"] = pd.Timestamp(
        "2026-01-02 14:00:00", tz="Asia/Shanghai"
    )
    audit["series_id"] = "series-1"
    audit["formula_id"] = "formula-1"
    audit["vintage_quality"] = "good"
    audit["staleness_trading_days"] = 0
    audit["lineage"] = "catalog-lineage"
    audit["lineage_hash"] = [
        f"lineage-hash-{index}" for index in range(len(audit))
    ]
    return audit


def _install_standard_sql(
    monkeypatch,
    *,
    values: pd.DataFrame | None = None,
    audit: pd.DataFrame | None = None,
):
    values = _standard_values() if values is None else values
    audit = _standard_audit(values) if audit is None else audit
    calls = []

    def fake_read_sql(sql, conn, *, params):
        calls.append((sql, dict(params or {})))
        if "/* cta-standard-values */" in sql:
            return values.copy()
        if "/* cta-standard-audit */" in sql:
            return audit.copy()
        raise AssertionError("unexpected SQL marker")

    monkeypatch.setattr(pg_source, "_read_sql", fake_read_sql)
    return calls


def _assert_standard_sql_contract(calls):
    assert len(calls) == 2
    sqls = [" ".join(sql.lower().split()) for sql, _ in calls]
    assert any("/* cta-standard-values */" in sql for sql in sqls)
    assert any("/* cta-standard-audit */" in sql for sql in sqls)
    for sql in sqls:
        assert "status = 'complete'" in sql
        assert "pit_mode = 'conservative'" in sql
        assert "build_version = (" in sql
        assert "order by finished_at desc" in sql
        assert "limit 1" in sql
        assert "available_at <= " in sql
        assert "trade_date::timestamp" in sql
        assert "time '15:00'" in sql
        assert "at time zone 'asia/shanghai'" in sql


def test_load_standard_fundamentals_pivots_values_and_returns_lineage(monkeypatch):
    values = _standard_values()
    audit_rows = _standard_audit(values)
    calls = _install_standard_sql(monkeypatch, values=values, audit=audit_rows)

    wide, audit, metadata = pg_source._load_standard_fundamentals(
        object(),
        start=None,
        end=None,
        symbols=["M", "CU"],
        schema="commodity_research",
    )

    assert set(wide.columns) == {
        "trade_date",
        "symbol",
        "spot",
        "basis_rate",
        "inventory",
        "profit",
    }
    m_row = wide.loc[wide["symbol"] == "M"].iloc[0]
    assert m_row[["spot", "basis_rate", "inventory", "profit"]].tolist() == [
        100.0,
        0.01,
        80.0,
        12.0,
    ]
    assert metadata == {
        "source": "standard",
        "pit_mode": "conservative",
        "build_version": "build-c-1",
        "catalog_version": "v1",
        "source_recorded_cutoff": pd.Timestamp("2026-01-01"),
        "schema": "commodity_research",
        "materialized_daily": True,
    }
    assert {
        "source_observation_date",
        "available_at",
        "series_id",
        "formula_id",
        "vintage_quality",
        "staleness_trading_days",
        "lineage",
        "lineage_hash",
    }.issubset(audit.columns)
    lineage = audit.set_index(["symbol", "metric"])
    assert lineage.loc[("M", "spot"), "lineage_hash"] == "lineage-hash-0"
    assert lineage.loc[("M", "spot"), "lineage"] == "catalog-lineage"
    _assert_standard_sql_contract(calls)


def test_load_standard_fundamentals_adds_optional_filters(monkeypatch):
    calls = _install_standard_sql(monkeypatch)
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)

    pg_source._load_standard_fundamentals(
        object(),
        start=start,
        end=end,
        symbols=["M", "CU"],
        schema="commodity_research",
    )

    for sql, params in calls:
        normalized = " ".join(sql.lower().split())
        assert "trade_date >= %(start)s" in normalized
        assert "trade_date <= %(end)s" in normalized
        assert "product_code = any(%(symbols)s)" in normalized
        assert params == {
            "start": start,
            "end": end,
            "symbols": ["M", "CU"],
        }


def test_load_standard_fundamentals_rejects_schema_before_sql(monkeypatch):
    calls = []

    def unexpected_sql(sql, conn, *, params):
        calls.append(sql)
        raise AssertionError("SQL must not execute")

    monkeypatch.setattr(pg_source, "_read_sql", unexpected_sql)

    with pytest.raises(ValueError, match="schema"):
        pg_source._load_standard_fundamentals(
            object(),
            start=None,
            end=None,
            symbols=["M"],
            schema="public",
        )

    assert calls == []


def test_load_standard_fundamentals_rejects_empty_result(monkeypatch):
    empty_values = pd.DataFrame()
    empty_audit = pd.DataFrame()
    _install_standard_sql(
        monkeypatch,
        values=empty_values,
        audit=empty_audit,
    )

    with pytest.raises(ValueError, match="no complete conservative build"):
        pg_source._load_standard_fundamentals(
            object(),
            start=None,
            end=None,
            symbols=["M"],
            schema="commodity_research",
        )


def test_load_standard_fundamentals_rejects_duplicate_value_rows(monkeypatch):
    values = _standard_values()
    values = pd.concat([values, values.iloc[[0]]], ignore_index=True)
    _install_standard_sql(monkeypatch, values=values)

    with pytest.raises(ValueError, match="duplicate"):
        pg_source._load_standard_fundamentals(
            object(),
            start=None,
            end=None,
            symbols=["M", "CU"],
            schema="commodity_research",
        )


def test_load_standard_fundamentals_rejects_multiple_build_versions(monkeypatch):
    values = _standard_values()
    values.loc[values.index[-1], "build_version"] = "build-c-2"
    _install_standard_sql(monkeypatch, values=values)

    with pytest.raises(ValueError, match="exactly one"):
        pg_source._load_standard_fundamentals(
            object(),
            start=None,
            end=None,
            symbols=["M", "CU"],
            schema="commodity_research",
        )


def test_load_standard_fundamentals_rejects_multiple_catalog_versions(monkeypatch):
    values = _standard_values()
    values.loc[values.index[-1], "catalog_version"] = "v2"
    _install_standard_sql(monkeypatch, values=values)

    with pytest.raises(ValueError, match="exactly one"):
        pg_source._load_standard_fundamentals(
            object(),
            start=None,
            end=None,
            symbols=["M", "CU"],
            schema="commodity_research",
        )


def test_load_standard_fundamentals_rejects_value_audit_build_mismatch(monkeypatch):
    values = _standard_values(build_version="build-c-1")
    audit = _standard_audit(values, build_version="build-c-2")
    _install_standard_sql(monkeypatch, values=values, audit=audit)

    with pytest.raises(ValueError, match="mismatch"):
        pg_source._load_standard_fundamentals(
            object(),
            start=None,
            end=None,
            symbols=["M", "CU"],
            schema="commodity_research",
        )


@pytest.mark.parametrize(
    ("column", "other_version"),
    [("build_version", "build-c-2"), ("catalog_version", "v2")],
)
def test_load_standard_fundamentals_rejects_multiple_audit_versions(
    monkeypatch,
    column,
    other_version,
):
    values = _standard_values()
    audit = _standard_audit(values)
    audit.loc[audit.index[-1], column] = other_version
    _install_standard_sql(monkeypatch, values=values, audit=audit)

    with pytest.raises(ValueError, match="exactly one"):
        pg_source._load_standard_fundamentals(
            object(),
            start=None,
            end=None,
            symbols=["M", "CU"],
            schema="commodity_research",
        )
