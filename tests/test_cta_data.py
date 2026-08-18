from __future__ import annotations

from datetime import date

import pandas as pd

from cta_gtja.data import CTADataSet


def test_slice_filters_standard_audit_by_symbol_and_date():
    before = date(2020, 1, 1)
    inside = date(2020, 1, 2)
    after = date(2020, 1, 3)
    audit = pd.DataFrame(
        [
            {
                "trade_date": before,
                "symbol": "CU",
                "metric": "basis_rate",
                "lineage": {"series_key": "cu_basis_before"},
            },
            {
                "trade_date": inside,
                "symbol": "CU",
                "metric": "basis_rate",
                "lineage": {"series_key": "cu_basis_inside"},
            },
            {
                "trade_date": inside,
                "symbol": "M",
                "metric": "inventory",
                "lineage": {"series_key": "m_inventory_inside"},
            },
            {
                "trade_date": after,
                "symbol": "CU",
                "metric": "profit",
                "lineage": {"series_key": "cu_profit_after"},
            },
        ]
    )
    metadata = {
        "source": "standard",
        "pit_mode": "conservative",
        "build_version": "build-c-1",
        "catalog_version": "v1",
        "materialized_daily": True,
    }
    data = CTADataSet(
        prices=pd.DataFrame(
            {
                "trade_date": [inside, inside],
                "symbol": ["CU", "M"],
                "open": [100.0, 200.0],
                "close": [101.0, 201.0],
            }
        ),
        fundamentals=pd.DataFrame(columns=["trade_date", "symbol"]),
        fundamental_quality=audit,
        fundamental_metadata=metadata,
    )
    original_audit = data.fundamental_quality.copy(deep=True)

    sliced = data.slice(symbols=["CU"], start=inside, end=inside)

    pd.testing.assert_frame_equal(
        sliced.fundamental_quality,
        audit.iloc[[1]],
    )
    assert sliced.fundamental_metadata == metadata
    assert sliced.fundamental_metadata is not metadata
    pd.testing.assert_frame_equal(data.fundamental_quality, original_audit)
