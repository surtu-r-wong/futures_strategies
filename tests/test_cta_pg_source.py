from __future__ import annotations

import pandas as pd
import pytest

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
    assert set(captured["params"]["excluded_symbols"]) == {
        "IF",
        "IC",
        "IH",
        "IM",
        "T",
        "TF",
        "TL",
        "TS",
    }


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
