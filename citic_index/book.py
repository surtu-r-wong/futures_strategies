"""From an index to a tradeable book: volatility scaling, a cap, and lots.

The replica is an index -- zero-sum rank weights with a gross of about 0.486,
no leverage and no cost.  Trading it means sizing that shape to a volatility
target and turning the result into contracts, which is what this module does
and what the daily runner calls.

Two conventions are carried over from the production carry runner rather than
reinvented, so the two books behave the same way where they face the same
question: the proportional gross cap (`cta_carry.risk.scale_weights`) and the
exchange order code (`cta_carry.targets.order_code`).

The scale at T is computed from returns through T and sizes the weights struck
at T, which earn T+1.  A window that reached into T+1 would size today's book
off tomorrow's move; a window shorter than its own length reads the volatility
wrong and levers against it, which is exactly how the carry runner over-levered
itself by 15.6% on 2026-09-11.
"""

import numpy as np
import pandas as pd

from cta_carry.risk import scale_weights
from cta_carry.targets import order_code


TARGET_COLUMNS = (
    "signal_date",
    "product",
    "contract",
    "order_code",
    "direction",
    "close",
    "multiplier",
    "raw_weight",
    "vol_scale",
    "target_weight",
    "notional",
    "lots",
)


def rolling_vol_scale(
    index_returns: pd.Series,
    *,
    vol_window: int = 252,
    target_vol: float = 0.15,
    min_observations: int | None = None,
) -> pd.Series:
    """`target_vol / realised vol` over a trailing window closing at each day."""
    if vol_window < 2:
        raise ValueError("vol_window must be at least 2")
    floor = vol_window if min_observations is None else min_observations
    if floor < 2 or floor > vol_window:
        raise ValueError("min_observations must be in [2, vol_window]")
    realised = (
        index_returns.rolling(vol_window, min_periods=floor).std(ddof=0)
        * np.sqrt(252.0)
    )
    return (target_vol / realised).where(realised > 0.0)


def lever(weights: pd.DataFrame, vol_scale: pd.Series, *, config) -> pd.DataFrame:
    """Scale each day's weights to target, under the proportional gross cap.

    A day whose scale is not yet defined emits nothing: an unsized book is not
    a small book.
    """
    if weights.empty:
        return weights.assign(vol_scale=[], target_weight=[])
    rows = []
    for trade_date, day in weights.groupby("trade_date", sort=True):
        scale = vol_scale.get(trade_date, float("nan"))
        if not np.isfinite(scale) or scale <= 0.0:
            continue
        scaled = scale_weights(
            dict(zip(day["product"], day["weight"])), float(scale), config
        )
        rows.append(
            day.assign(
                vol_scale=float(scale),
                target_weight=day["product"].map(scaled).astype(float),
            )
        )
    if not rows:
        return weights.iloc[0:0].assign(vol_scale=[], target_weight=[])
    return pd.concat(rows, ignore_index=True)


def next_targets(
    levered: pd.DataFrame,
    legs: pd.DataFrame,
    pool: pd.DataFrame,
    *,
    signal_date,
    capital: float,
) -> pd.DataFrame:
    """The contracts and lots to hold into the open after `signal_date`.

    A product that cannot be priced or sized is refused rather than dropped:
    a sheet short one leg still looks complete, and 3.4 says the index trades
    the dominant contract, so there is no substitute to fall back on.
    """
    if not np.isfinite(capital) or capital <= 0.0:
        raise ValueError("capital must be finite and positive")
    day = levered.loc[levered["trade_date"] == signal_date].copy()
    if day.empty:
        return pd.DataFrame(columns=list(TARGET_COLUMNS))

    for frame, columns, what in (
        (legs, ["main_contract", "main_close"], "a dominant contract bar"),
        (pool, ["multiplier"], "a contract multiplier"),
    ):
        slice_ = frame.loc[frame["trade_date"] == signal_date, ["product"] + columns]
        day = day.merge(slice_, on="product", how="left")
        missing = sorted(day.loc[day[columns[0]].isna(), "product"])
        if missing:
            raise ValueError(
                f"{signal_date}: no {what} for {', '.join(missing)}"
                " -- refusing the sheet rather than emitting it short a leg"
            )

    day = day.rename(columns={"main_contract": "contract", "main_close": "close"})
    day["signal_date"] = signal_date
    day["order_code"] = day["contract"].map(order_code)
    day["direction"] = np.sign(day["target_weight"]).astype(int)
    day["raw_weight"] = day["weight"]
    day["notional"] = day["target_weight"] * float(capital)
    day["lots"] = (day["notional"] / (day["close"] * day["multiplier"])).round().astype("Int64")
    day = day.sort_values("product", kind="mergesort")
    return day.loc[:, list(TARGET_COLUMNS)].reset_index(drop=True)
