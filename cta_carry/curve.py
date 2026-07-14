from dataclasses import dataclass
from datetime import date
import re


_CONTRACT_PATTERN = re.compile(
    r"(?P<product>[A-Za-z]+)(?P<digits>[0-9]{3}|[0-9]{4})"
    r"(?:\.(?P<exchange>[A-Za-z]+))?"
)


class ContractParseError(ValueError):
    """Raised when a contract symbol cannot be resolved unambiguously."""


@dataclass(frozen=True)
class ContractId:
    product: str
    exchange_suffix: str
    delivery_year: int
    delivery_month: int
    normalized: str

    @property
    def delivery_yyyymm(self) -> int:
        return self.delivery_year * 100 + self.delivery_month


def _months_from_trade_date(year: int, month: int, trade_date: date) -> int:
    return (year - trade_date.year) * 12 + month - trade_date.month


def _candidate_years(digits: str, trade_date: date) -> list[int]:
    if len(digits) == 4:
        year_in_century = int(digits[:2])
        trade_century = trade_date.year // 100 * 100
        return [
            trade_century - 100 + year_in_century,
            trade_century + year_in_century,
            trade_century + 100 + year_in_century,
        ]

    year_digit = int(digits[0])
    return [
        year
        for year in range(trade_date.year - 10, trade_date.year + 11)
        if year % 10 == year_digit
    ]


def parse_contract(symbol: str, trade_date: date) -> ContractId:
    if not isinstance(symbol, str):
        raise ContractParseError("contract symbol must be a string")

    match = _CONTRACT_PATTERN.fullmatch(symbol)
    if match is None:
        raise ContractParseError(f"invalid contract symbol: {symbol!r}")

    product = match.group("product").upper()
    digits = match.group("digits")
    exchange = (match.group("exchange") or "").upper()
    delivery_month = int(digits[-2:])
    if not 1 <= delivery_month <= 12:
        raise ContractParseError(f"invalid delivery month in contract: {symbol!r}")

    candidates = [
        year
        for year in _candidate_years(digits, trade_date)
        if 0 <= _months_from_trade_date(year, delivery_month, trade_date) <= 120
    ]
    if len(candidates) != 1:
        raise ContractParseError(
            f"delivery year is not uniquely resolvable for contract: {symbol!r}"
        )

    normalized = f"{product}{digits}"
    if exchange:
        normalized = f"{normalized}.{exchange}"

    return ContractId(
        product=product,
        exchange_suffix=exchange,
        delivery_year=candidates[0],
        delivery_month=delivery_month,
        normalized=normalized,
    )
