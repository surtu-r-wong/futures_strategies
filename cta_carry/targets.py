"""Next-open target sizing helpers for the daily run."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


TARGET_COLUMNS = (
    "signal_date",
    "product",
    "contract",
    "order_code",
    "code_exchange",
    "code_qmt",
    "code_ctp",
    "direction",
    "carry_ma",
    "close",
    "raw_weight",
    "vol_scale",
    "target_weight",
    "current_weight",
    "weight_change",
    "reason",
)

# Panel suffix -> (QMT market, CTP exchange id).
EXCHANGE_MARKETS = {
    "SHF": ("SF", "SHFE"),
    "DCE": ("DF", "DCE"),
    "CZC": ("ZF", "CZCE"),
    "INE": ("INE", "INE"),
    "GFE": ("GF", "GFEX"),
}


def _split(contract: str) -> tuple[str, str | None]:
    code, sep, exchange = contract.partition(".")
    return code, (exchange if sep else None)


def _czce_three_digit(code: str) -> str:
    """Zhengzhou quotes a one-digit delivery year: PL2611 -> PL611, TA611 unchanged."""
    letters = code.rstrip("0123456789")
    digits = code[len(letters) :]
    return f"{letters}{digits[1:] if len(digits) == 4 else digits}"


def wind_code(contract: str) -> str:
    """The Wind terminal code: the panel's `<PRODUCT><YYMM>.<EXCH>` with Zhengzhou's
    year cut to one digit (`CF2701.CZC` -> `CF701.CZC`, which is what Wind's data
    functions accept; the four-digit form the database stores is refused there).
    A contract without a suffix (synthetic panels) is returned unchanged."""
    code, exchange = _split(contract)
    if exchange is None:
        return contract
    if exchange == "CZC":
        code = _czce_three_digit(code)
    return f"{code}.{exchange}"


def exchange_code(contract: str) -> str:
    """The exchange's own instrument id: Zhengzhou upper case with a one-digit
    year (`CF701`); DCE, SHFE, INE and GFEX lower case with four (`rb2611`)."""
    code, exchange = _split(contract)
    if exchange is None:
        return contract
    if exchange == "CZC":
        return _czce_three_digit(code)
    return code.lower()


def qmt_code(contract: str) -> str:
    """迅投 QMT / xtquant: the exchange id plus its market (`rb2611.SF`, `CF701.ZF`)."""
    _, exchange = _split(contract)
    market = EXCHANGE_MARKETS.get(exchange or "")
    if market is None:
        return contract
    return f"{exchange_code(contract)}.{market[0]}"


def ctp_code(contract: str) -> str:
    """CTP-style `InstrumentID.ExchangeID` (vn.py's spelling): `rb2611.SHFE`, `CF701.CZCE`."""
    _, exchange = _split(contract)
    market = EXCHANGE_MARKETS.get(exchange or "")
    if market is None:
        return contract
    return f"{exchange_code(contract)}.{market[1]}"


# The sheet carries one column per convention (user ruling 2026-09-16, after a
# Wind-only column proved unusable for the desk's other tools and the earlier
# exchange-id column unusable for Wind).  `order_code` is the Wind spelling.
CODE_COLUMNS = {
    "order_code": wind_code,
    "code_exchange": exchange_code,
    "code_qmt": qmt_code,
    "code_ctp": ctp_code,
}


def order_code(contract: str) -> str:
    """The Wind code; see `CODE_COLUMNS` for the other spellings."""
    return wind_code(contract)


def code_columns(contract: str) -> dict[str, str]:
    """Every spelling of one contract, keyed by sheet column."""
    return {name: fn(contract) for name, fn in CODE_COLUMNS.items()}


def infer_product_multipliers(prices: pd.DataFrame, *, window: int = 60) -> dict[str, float]:
    """Median of turnover / (volume * close) over each product's last traded bars.

    The daily path carries no multiplier metadata.  A short trailing window
    (not the whole history) keeps the estimate current if an exchange changes
    a multiplier; the median shrugs off odd bars.  Products with no traded bar
    are absent from the result.
    """
    traded = prices.loc[(prices["volume"] > 0) & (prices["turnover"] > 0)]
    if traded.empty:
        return {}
    ordered = traded.sort_values(["product", "trade_date"], kind="mergesort")
    ratio = ordered["turnover"] / (ordered["volume"] * ordered["close"])
    out: dict[str, float] = {}
    for product, values in ratio.groupby(ordered["product"], sort=True):
        tail = values.tail(window)
        if len(tail):
            out[str(product)] = float(tail.median())
    return out


def lots_for_targets(
    targets: pd.DataFrame,
    *,
    capital: float,
    multipliers: dict[str, float],
) -> pd.DataFrame:
    """Add multiplier, notional and rounded lots to a next-target frame.

    lots = target_weight * capital / (close * multiplier), rounded to the
    nearest whole contract; a product without a multiplier gets NaN so the
    gap is visible rather than silently zero.
    """
    if not math.isfinite(capital) or capital <= 0.0:
        raise ValueError("capital must be finite and positive")
    sized = targets.copy()
    sized["multiplier"] = sized["product"].map(multipliers).astype(float)
    sized["notional"] = sized["target_weight"] * float(capital)
    contract_value = sized["close"] * sized["multiplier"]
    raw_lots = sized["notional"] / contract_value
    lots = raw_lots.where(np.isfinite(raw_lots)).round()
    sized["lots"] = lots.astype("Int64")
    return sized
