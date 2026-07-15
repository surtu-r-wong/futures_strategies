from dataclasses import dataclass
from datetime import date
import re

import pandas as pd


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

    candidates = sorted(
        (delta, year)
        for year in _candidate_years(digits, trade_date)
        if 0
        <= (delta := _months_from_trade_date(year, delivery_month, trade_date))
        <= 120
    )
    if not candidates:
        raise ContractParseError(
            f"delivery year is not uniquely resolvable for contract: {symbol!r}"
        )
    nearest_delta, delivery_year = candidates[0]
    if len(candidates) > 1 and candidates[1][0] == nearest_delta:
        raise ContractParseError(
            f"delivery year is not uniquely resolvable for contract: {symbol!r}"
        )

    normalized = f"{product}{digits}"
    if exchange:
        normalized = f"{normalized}.{exchange}"

    return ContractId(
        product=product,
        exchange_suffix=exchange,
        delivery_year=delivery_year,
        delivery_month=delivery_month,
        normalized=normalized,
    )


CURVE_AUDIT_COLUMNS = (
    "trade_date",
    "product",
    "contract",
    "in_pool",
    "role",
    "selected",
    "reason",
    "liquidity_mean",
)
_CURVE_COLUMNS = (
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
)
_LIQUIDITY_COLUMNS = (
    "trade_date",
    "product",
    "product_turnover",
    "liquidity_mean",
    "in_pool",
)


@dataclass(frozen=True)
class CurveResult:
    curve: pd.DataFrame
    audit: pd.DataFrame


def aggregate_product_liquidity(prices: pd.DataFrame, config) -> pd.DataFrame:
    """Aggregate product turnover and apply shifted liquidity eligibility."""
    if prices.empty:
        return pd.DataFrame(columns=_LIQUIDITY_COLUMNS)

    liquidity = (
        prices.groupby(
            ["product", "trade_date"],
            as_index=False,
            sort=False,
        )["turnover"]
        .sum()
        .rename(columns={"turnover": "product_turnover"})
    )
    liquidity = liquidity.sort_values(
        ["product", "trade_date"],
        kind="mergesort",
    ).reset_index(drop=True)
    liquidity["liquidity_mean"] = liquidity.groupby(
        "product",
        sort=False,
    )["product_turnover"].transform(
        lambda values: values.rolling(
            config.liquidity_window,
            min_periods=config.liquidity_window,
        )
        .mean()
        .shift(1)
    )
    liquidity["in_pool"] = (
        liquidity["liquidity_mean"].notna()
        & (
            liquidity["liquidity_mean"]
            >= config.liquidity_threshold
        )
    )
    return liquidity.loc[:, _LIQUIDITY_COLUMNS].sort_values(
        ["trade_date", "product"],
        kind="mergesort",
    ).reset_index(drop=True)


def _month_gap(near_delivery_yyyymm, far_delivery_yyyymm):
    near_year, near_month = divmod(int(near_delivery_yyyymm), 100)
    far_year, far_month = divmod(int(far_delivery_yyyymm), 100)
    gap = (far_year - near_year) * 12 + far_month - near_month
    if gap <= 0:
        raise ValueError("far delivery must be strictly later than near delivery")
    return gap


def _empty_curve():
    return pd.DataFrame(columns=_CURVE_COLUMNS)


def _empty_curve_audit():
    return pd.DataFrame(columns=CURVE_AUDIT_COLUMNS)


def _append_audit(
    audit_rows,
    candidates,
    *,
    in_pool,
    liquidity_mean,
    reason,
):
    for candidate in candidates.itertuples(index=False):
        audit_rows.append(
            {
                "trade_date": candidate.trade_date,
                "product": candidate.product,
                "contract": candidate.contract,
                "in_pool": in_pool,
                "role": "candidate",
                "selected": False,
                "reason": reason,
                "liquidity_mean": liquidity_mean,
            }
        )


def build_curve(prices: pd.DataFrame, config) -> CurveResult:
    """Build deterministic main/secondary contract Carry curves."""
    if prices.empty:
        return CurveResult(curve=_empty_curve(), audit=_empty_curve_audit())

    liquidity = aggregate_product_liquidity(prices, config)
    candidates_by_day = prices.merge(
        liquidity,
        on=["trade_date", "product"],
        how="left",
        validate="many_to_one",
    )
    candidates_by_day = candidates_by_day.sort_values(
        ["trade_date", "product", "contract"],
        kind="mergesort",
    ).reset_index(drop=True)

    curve_rows = []
    audit_rows = []
    for _, candidates in candidates_by_day.groupby(
        ["trade_date", "product"],
        sort=False,
    ):
        liquidity_mean = candidates["liquidity_mean"].iloc[0]
        in_pool = bool(candidates["in_pool"].iloc[0])
        if not in_pool:
            reason = (
                "liquidity_history_incomplete"
                if pd.isna(liquidity_mean)
                else "below_liquidity_threshold"
            )
            _append_audit(
                audit_rows,
                candidates,
                in_pool=False,
                liquidity_mean=liquidity_mean,
                reason=reason,
            )
            continue

        ranked = candidates.sort_values(
            ["oi", "volume", "contract"],
            ascending=[False, False, True],
            kind="mergesort",
        )
        main = ranked.iloc[0]
        later = ranked.loc[
            ranked["delivery_yyyymm"]
            > main["delivery_yyyymm"]
        ]
        if later.empty:
            _append_audit(
                audit_rows,
                candidates,
                in_pool=True,
                liquidity_mean=liquidity_mean,
                reason="no_strictly_later_contract",
            )
            continue

        secondary = later.iloc[0]
        month_gap = _month_gap(
            main["delivery_yyyymm"],
            secondary["delivery_yyyymm"],
        )
        carry_raw = (
            (main["close"] / secondary["close"] - 1.0)
            * 12.0
            / month_gap
        )
        curve_rows.append(
            {
                "trade_date": main["trade_date"],
                "product": main["product"],
                "main_contract": main["contract"],
                "secondary_contract": secondary["contract"],
                "main_delivery_yyyymm": main["delivery_yyyymm"],
                "secondary_delivery_yyyymm": secondary[
                    "delivery_yyyymm"
                ],
                "month_gap": month_gap,
                "main_close": main["close"],
                "secondary_close": secondary["close"],
                "main_volume": main["volume"],
                "main_oi": main["oi"],
                "product_turnover": main["product_turnover"],
                "liquidity_mean": liquidity_mean,
                "carry_raw": carry_raw,
            }
        )

        for candidate in candidates.itertuples(index=False):
            if candidate.contract == main["contract"]:
                role = "main"
                selected = True
                reason = "highest_oi"
            elif candidate.contract == secondary["contract"]:
                role = "secondary"
                selected = True
                reason = "later_highest_oi"
            else:
                role = "candidate"
                selected = False
                reason = "not_selected"
            audit_rows.append(
                {
                    "trade_date": candidate.trade_date,
                    "product": candidate.product,
                    "contract": candidate.contract,
                    "in_pool": True,
                    "role": role,
                    "selected": selected,
                    "reason": reason,
                    "liquidity_mean": liquidity_mean,
                }
            )

    curve = pd.DataFrame(curve_rows, columns=_CURVE_COLUMNS)
    if not curve.empty:
        curve = curve.sort_values(
            ["trade_date", "product"],
            kind="mergesort",
        ).reset_index(drop=True)
        curve["carry_ma"] = curve.groupby(
            "product",
            sort=False,
        )["carry_raw"].transform(
            lambda values: values.rolling(
                config.carry_window,
                min_periods=config.carry_window,
            ).mean()
        )
        curve = curve.loc[:, _CURVE_COLUMNS]

    audit = pd.DataFrame(audit_rows, columns=CURVE_AUDIT_COLUMNS)
    if not audit.empty:
        audit = audit.sort_values(
            ["trade_date", "product", "contract"],
            kind="mergesort",
        ).reset_index(drop=True)

    return CurveResult(curve=curve, audit=audit)
