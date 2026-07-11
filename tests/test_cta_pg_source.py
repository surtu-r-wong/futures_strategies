from __future__ import annotations

import pandas as pd
import pytest

from cta_gtja.pg_source import _apply_adjustment_policy


def _prices():
    return pd.DataFrame([
        {
            "trade_date": "2026-01-02",
            "symbol": "M",
            "contract": "M2601",
            "open_raw": 10.0,
            "open_ba": 100.0,
            "open_fa": -1.0,
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
            "close_raw": 21.0,
            "close_ba": -5.0,
            "close_fa": -6.0,
            "volume": 2000,
            "open_interest": 300,
        },
    ])


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
