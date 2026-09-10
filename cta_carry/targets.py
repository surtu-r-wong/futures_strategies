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


def order_code(contract: str) -> str:
    """The exchange's own instrument id for a `<PRODUCT><YYMM>.<EXCH>` contract.

    CZCE quotes a one-digit year (PL2611 -> PL611, upper case); DCE, SHFE, INE
    and GFEX ids are the four-digit code in lower case. A contract without an
    exchange suffix (synthetic panels) is returned unchanged.
    """
    code, sep, exchange = contract.partition(".")
    if not sep:
        return contract
    if exchange == "CZC":
        letters = code.rstrip("0123456789")
        digits = code[len(letters) :]
        return f"{letters}{digits[1:] if len(digits) == 4 else digits}"
    return code.lower()


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
