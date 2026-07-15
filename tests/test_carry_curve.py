from dataclasses import FrozenInstanceError
from datetime import date

import pandas as pd
import pytest

from cta_carry.config import CarryConfig
from cta_carry.curve import (
    CURVE_AUDIT_COLUMNS,
    ContractId,
    ContractParseError,
    CurveResult,
    _month_gap,
    aggregate_product_liquidity,
    build_curve,
    parse_contract,
)
from cta_carry.data import normalize_contract_daily


@pytest.mark.parametrize(
    ("symbol", "trade_date", "expected"),
    [
        (
            "rb2410.shf",
            date(2024, 1, 2),
            ContractId("RB", "SHF", 2024, 10, "RB2410.SHF"),
        ),
        (
            "TA409.CZC",
            date(2024, 1, 2),
            ContractId("TA", "CZC", 2024, 9, "TA409.CZC"),
        ),
        (
            "CF001.CZC",
            date(2019, 12, 2),
            ContractId("CF", "CZC", 2020, 1, "CF001.CZC"),
        ),
        (
            "CU9912.SHF",
            date(1999, 1, 4),
            ContractId("CU", "SHF", 1999, 12, "CU9912.SHF"),
        ),
        (
            "m2501",
            date(2024, 5, 6),
            ContractId("M", "", 2025, 1, "M2501"),
        ),
    ],
)
def test_parse_contract_resolves_delivery_date_from_trade_date(
    symbol: str, trade_date: date, expected: ContractId
) -> None:
    assert parse_contract(symbol, trade_date) == expected


def test_parse_contract_prefers_nearest_three_digit_delivery_year() -> None:
    parsed = parse_contract("TA001.CZC", date(2020, 1, 15))

    assert parsed.delivery_yyyymm == 202001


def test_contract_id_is_frozen_and_exposes_delivery_yyyymm() -> None:
    contract = ContractId("RB", "SHF", 2024, 10, "RB2410.SHF")

    assert contract.delivery_yyyymm == 202410
    with pytest.raises(FrozenInstanceError):
        contract.delivery_month = 11


@pytest.mark.parametrize(
    "symbol",
    ["RB2413.SHF", "2410.SHF", "RB", "RB1901.SHF"],
)
def test_parse_contract_rejects_invalid_or_unresolvable_symbols(symbol: str) -> None:
    with pytest.raises(ContractParseError):
        parse_contract(symbol, date(2024, 1, 2))


def test_contract_parse_error_is_a_value_error() -> None:
    assert issubclass(ContractParseError, ValueError)


CURVE_COLUMNS = [
    "trade_date",
    "product",
    "main_contract",
    "secondary_contract",
    "main_delivery_yyyymm",
    "secondary_delivery_yyyymm",
    "month_gap",
    "main_close",
    "secondary_close",
    "main_volume",
    "main_oi",
    "product_turnover",
    "liquidity_mean",
    "carry_raw",
    "carry_ma",
]


def _bar(
    trade_date,
    contract,
    *,
    close=100.0,
    volume=100.0,
    oi=100.0,
    turnover=1_000.0,
):
    return {
        "trade_date": trade_date,
        "contract": contract,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
        "oi": oi,
        "turnover": turnover,
    }


def _prices(rows):
    return normalize_contract_daily(pd.DataFrame(rows)).prices


def test_liquidity_pool_uses_complete_shifted_product_history() -> None:
    dates = pd.bdate_range("2024-01-02", periods=4).date.tolist()
    prices = _prices(
        [
            _bar(
                trade_date,
                contract,
                close=close,
                volume=volume,
                oi=oi,
                turnover=6_000_000_000.0,
            )
            for trade_date in dates
            for contract, close, volume, oi in (
                ("RB2405.SHF", 110.0, 200.0, 300.0),
                ("RB2410.SHF", 120.0, 100.0, 200.0),
            )
        ]
    )
    config = CarryConfig(
        liquidity_window=2,
        liquidity_threshold=10_000_000_000.0,
        carry_window=1,
    )

    liquidity = aggregate_product_liquidity(prices, config)
    result = build_curve(prices, config)

    assert liquidity["product_turnover"].tolist() == [12_000_000_000.0] * 4
    assert liquidity["in_pool"].tolist() == [False, False, True, True]
    assert result.curve["trade_date"].tolist() == dates[2:]
    assert result.curve.iloc[0]["liquidity_mean"] == 12_000_000_000.0


def test_curve_selects_later_secondary_after_oi_and_volume_ranking() -> None:
    dates = pd.bdate_range("2024-01-02", periods=2).date.tolist()
    contracts = (
        ("RB2401.SHF", 100.0, 999.0, 200.0),
        ("RB2405.SHF", 105.0, 100.0, 300.0),
        ("RB2410.SHF", 110.0, 200.0, 300.0),
        ("RB2501.SHF", 120.0, 300.0, 250.0),
    )
    prices = _prices(
        [
            _bar(
                trade_date,
                contract,
                close=close,
                volume=volume,
                oi=oi,
            )
            for trade_date in dates
            for contract, close, volume, oi in contracts
        ]
    )
    config = CarryConfig(
        liquidity_window=1,
        liquidity_threshold=0.0,
        carry_window=1,
    )

    curve = build_curve(prices, config).curve

    assert len(curve) == 1
    row = curve.iloc[0]
    assert row["main_contract"] == "RB2410.SHF"
    assert row["secondary_contract"] == "RB2501.SHF"
    assert row["month_gap"] == 3
    assert row["carry_raw"] == pytest.approx(
        (110.0 / 120.0 - 1.0) * 12 / 3
    )


def test_carry_ma_uses_complete_valid_curve_days_and_audits_roles() -> None:
    dates = pd.bdate_range("2024-01-02", periods=4).date.tolist()
    prices = _prices(
        [
            _bar(
                trade_date,
                contract,
                close=close,
                volume=volume,
                oi=oi,
            )
            for trade_date in dates
            for contract, close, volume, oi in (
                ("M2405.DCE", 100.0, 200.0, 300.0),
                ("M2409.DCE", 110.0, 100.0, 200.0),
            )
        ]
    )
    config = CarryConfig(
        liquidity_window=1,
        liquidity_threshold=0.0,
        carry_window=2,
    )

    result = build_curve(prices, config)

    assert result.curve["carry_ma"].isna().tolist() == [True, False, False]
    successful_audit = result.audit[result.audit["selected"]]
    assert set(successful_audit["role"]) == {"main", "secondary"}


def test_curve_breaks_equal_oi_and_volume_ties_by_normalized_contract() -> None:
    dates = pd.bdate_range("2024-01-02", periods=2).date.tolist()
    prices = _prices(
        [
            _bar(trade_date, contract, volume=200.0, oi=300.0)
            for trade_date in dates
            for contract in ("RB2410.SHF", "RB2405.SHF")
        ]
    )
    config = CarryConfig(
        liquidity_window=1,
        liquidity_threshold=0.0,
        carry_window=1,
    )

    curve = build_curve(prices, config).curve

    assert curve["main_contract"].tolist() == ["RB2405.SHF"]
    assert curve["secondary_contract"].tolist() == ["RB2410.SHF"]


def test_curve_audits_incomplete_and_below_threshold_pool_days() -> None:
    dates = pd.bdate_range("2024-01-02", periods=2).date.tolist()
    prices = _prices(
        [
            _bar(trade_date, contract, turnover=0.0)
            for trade_date in dates
            for contract in ("RB2405.SHF", "RB2410.SHF")
        ]
    )
    config = CarryConfig(
        liquidity_window=1,
        liquidity_threshold=1.0,
        carry_window=1,
    )

    result = build_curve(prices, config)

    assert result.curve.empty
    assert result.curve.columns.tolist() == CURVE_COLUMNS
    assert result.audit["reason"].tolist() == [
        "liquidity_history_incomplete",
        "liquidity_history_incomplete",
        "below_liquidity_threshold",
        "below_liquidity_threshold",
    ]
    assert result.audit["in_pool"].tolist() == [False] * 4


def test_curve_audits_all_candidates_when_no_contract_is_strictly_later() -> None:
    dates = pd.bdate_range("2024-01-02", periods=2).date.tolist()
    prices = _prices(
        [
            _bar(
                trade_date,
                contract,
                volume=volume,
                oi=oi,
            )
            for trade_date in dates
            for contract, volume, oi in (
                ("RB2405.SHF", 100.0, 200.0),
                ("RB2410.SHF", 200.0, 300.0),
            )
        ]
    )
    config = CarryConfig(
        liquidity_window=1,
        liquidity_threshold=0.0,
        carry_window=1,
    )

    result = build_curve(prices, config)
    audit = result.audit[result.audit["trade_date"] == dates[1]]

    assert result.curve.empty
    assert audit["reason"].tolist() == [
        "no_strictly_later_contract",
        "no_strictly_later_contract",
    ]
    assert audit["role"].tolist() == ["candidate", "candidate"]
    assert audit["selected"].tolist() == [False, False]


def test_empty_curve_result_is_frozen_and_preserves_stable_columns() -> None:
    config = CarryConfig(
        liquidity_window=1,
        liquidity_threshold=0.0,
        carry_window=1,
    )

    result = build_curve(pd.DataFrame(), config)

    assert isinstance(result, CurveResult)
    assert result.curve.columns.tolist() == CURVE_COLUMNS
    assert result.audit.columns.tolist() == list(CURVE_AUDIT_COLUMNS)
    with pytest.raises(FrozenInstanceError):
        result.curve = pd.DataFrame()


def test_month_gap_handles_year_boundaries_and_rejects_nonpositive_gaps() -> None:
    assert _month_gap(202410, 202501) == 3
    with pytest.raises(ValueError):
        _month_gap(202501, 202501)
    with pytest.raises(ValueError):
        _month_gap(202502, 202501)
