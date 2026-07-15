"""Pure cross-sectional signal construction for the Carry strategy."""
from dataclasses import dataclass
from datetime import date
import math

import numpy as np
import pandas as pd


_SIGNAL_COLUMNS = (
    "trade_date",
    "product",
    "main_contract",
    "main_close",
    "main_volume",
    "main_oi",
    "carry_ma",
    "atr",
    "price_ma",
    "volume_ma",
    "oi_ma",
    "input_ready",
    "rank_direction",
    "strength",
    "effective_direction",
    "reason",
)


@dataclass(frozen=True)
class SignalResult:
    signals: pd.DataFrame
    signal_ready_date: date | None


def _empty_signals() -> pd.DataFrame:
    return pd.DataFrame(columns=_SIGNAL_COLUMNS)


def _finite_mask(values: pd.Series) -> pd.Series:
    return np.isfinite(values).fillna(False).astype(bool)


def build_signals(curve_with_atr: pd.DataFrame, config) -> SignalResult:
    """Build point-in-time cross-sectional Carry signals."""
    if curve_with_atr.empty:
        return SignalResult(
            signals=_empty_signals(),
            signal_ready_date=None,
        )

    signals = curve_with_atr.sort_values(
        ["product", "trade_date"],
        kind="mergesort",
    ).copy()
    for source, target in (
        ("main_close", "price_ma"),
        ("main_volume", "volume_ma"),
        ("main_oi", "oi_ma"),
    ):
        signals[target] = signals.groupby(
            "product",
            sort=False,
        )[source].transform(
            lambda values: values.rolling(
                config.momentum_window,
                min_periods=config.momentum_window,
            ).mean()
        )

    input_ready = pd.Series(True, index=signals.index)
    for column in (
        "carry_ma",
        "price_ma",
        "volume_ma",
        "oi_ma",
        "atr",
    ):
        input_ready &= _finite_mask(signals[column])
    input_ready &= signals["atr"].gt(0.0).fillna(False)
    signals["input_ready"] = input_ready.astype(bool)

    signals = signals.sort_values(
        ["trade_date", "product"],
        kind="mergesort",
    ).reset_index(drop=True)
    signals["rank_direction"] = 0
    signals["strength"] = 0.0
    signals["effective_direction"] = 0
    signals["reason"] = ""

    signal_ready_date = None
    for trade_date, daily in signals.groupby(
        "trade_date",
        sort=False,
    ):
        ready = daily.loc[daily["input_ready"]].sort_values(
            ["carry_ma", "product"],
            kind="mergesort",
        )
        if len(ready) < 5:
            signals.loc[daily.index, "reason"] = "insufficient_cross_section"
            continue

        if signal_ready_date is None:
            signal_ready_date = trade_date
        signals.loc[daily.index, "reason"] = "rank_and_filter"

        selection_count = max(
            1,
            math.floor(len(ready) * config.selection_fraction),
        )
        bottom = ready.head(selection_count)
        top = ready.tail(selection_count)
        long_indexes = bottom.loc[bottom["carry_ma"] < 0.0].index
        short_indexes = top.loc[top["carry_ma"] > 0.0].index
        signals.loc[long_indexes, "rank_direction"] = 1
        signals.loc[short_indexes, "rank_direction"] = -1

        for index in daily.index:
            if (
                not signals.at[index, "input_ready"]
                or signals.at[index, "rank_direction"] == 0
            ):
                continue

            direction = signals.at[index, "rank_direction"]
            main_close = signals.at[index, "main_close"]
            price_ma = signals.at[index, "price_ma"]
            trend_aligned = (
                direction == 1
                and main_close > price_ma
            ) or (
                direction == -1
                and main_close < price_ma
            )
            trend_opposed = (
                direction == 1
                and main_close < price_ma
            ) or (
                direction == -1
                and main_close > price_ma
            )
            if trend_aligned:
                strength = 1.0
            elif (
                trend_opposed
                and signals.at[index, "main_volume"]
                < signals.at[index, "volume_ma"]
                and signals.at[index, "main_oi"]
                < signals.at[index, "oi_ma"]
            ):
                strength = 0.5
            else:
                strength = 0.0

            signals.at[index, "strength"] = strength
            if strength > 0.0:
                signals.at[index, "effective_direction"] = direction

    return SignalResult(
        signals=signals.loc[:, list(_SIGNAL_COLUMNS)],
        signal_ready_date=signal_ready_date,
    )
