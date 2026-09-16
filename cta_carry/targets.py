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
    """The code an order ticket carries: the Wind contract code, suffix and all.

    `RB2611.SHF`, `CF2701.CZC`, `LC2701.GFE` go out exactly as the panel stores
    them (user ruling 2026-09-16: the desk keys orders by the Wind code, and a
    bare exchange instrument id -- `rb2611`, `CF701` -- could not be used
    directly). Kept as a function so the convention has one home; a contract
    without a suffix (synthetic panels) is returned unchanged too.
    """
    return contract


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
