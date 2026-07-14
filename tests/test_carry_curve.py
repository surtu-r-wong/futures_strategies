from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from cta_carry.curve import ContractId, ContractParseError, parse_contract


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
