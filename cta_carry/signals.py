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
    "trend_state",
    "rank_direction",
    "strength",
    "effective_direction",
    "reason",
)


# Present only when the basis-momentum leg is switched on, so a configuration
# that leaves it off produces exactly the frame it produced before.
_BLEND_COLUMNS = (
    "basis_momentum",
    "bmom_ready",
    "bmom_weight",
    "blend_weight",
)


@dataclass(frozen=True)
class SignalResult:
    signals: pd.DataFrame
    signal_ready_date: date | None


def _empty_signals() -> pd.DataFrame:
    return pd.DataFrame(columns=_SIGNAL_COLUMNS)


def _finite_mask(values: pd.Series) -> pd.Series:
    return np.isfinite(values).fillna(False).astype(bool)


def rank_linear_weights(ready: pd.DataFrame, column: str = "carry_ma") -> pd.Series:
    """CITIC 3.5 step 3: w = (rank - (1+N)/2) / (N(1+N)/2), `column` ascending.

    Zero-sum by construction; the median product of an odd cross-section gets
    exactly 0.  Ties resolve in row order, so pass a frame already sorted by
    (column, product) for determinism.  The basis-momentum leg ranks the same
    way on `basis_momentum`, which is why the column is a parameter.
    """
    count = len(ready)
    rank = ready[column].rank(method="first", ascending=True)
    return (rank - (1 + count) / 2) / (count * (1 + count) / 2)


def _rebalance_dates(trade_dates: pd.Series, cadence: str) -> set:
    """Dates on which the basis-momentum leg re-ranks.

    "monthly" takes the first trade date PRESENT in each calendar month, so a
    month whose first day is missing -- a holiday, or a day the coverage gate
    truncated away -- rebalances on the next day that is there rather than
    skipping the month or re-ranking on stale data.
    """
    ordered = pd.Series(sorted(pd.unique(trade_dates)))
    if cadence == "daily":
        return set(ordered)
    months = pd.to_datetime(ordered).dt.to_period("M")
    return set(ordered.groupby(months).first())


def _trend_states(signals: pd.DataFrame, config) -> list:
    """Carry a per-product trend state forward across a product-ordered frame.

    The state flips only when the close clears `trend_band_atr` ATRs beyond the
    momentum MA; inside the band it holds.  A zero band leaves no band to sit
    inside, so the state degenerates to the original stateless MA comparison --
    including close == price_ma resolving to 0 rather than holding, which a
    limit-locked contract really does produce.
    """
    band = float(config.trend_band_atr)
    confirm = int(config.trend_confirm_days)
    states: list = []
    current: dict = {}
    streaks: dict = {}
    for product, close, price_ma, atr in zip(
        signals["product"],
        signals["main_close"],
        signals["price_ma"],
        signals["atr"],
    ):
        state = current.get(product, 0)
        if _is_finite(close) and _is_finite(price_ma) and _is_finite(atr) and atr > 0.0:
            half_width = band * atr
            if close > price_ma + half_width:
                side = 1
            elif close < price_ma - half_width:
                side = -1
            else:
                side = 0

            previous_side, run_length = streaks.get(product, (0, 0))
            if side != 0:
                run_length = run_length + 1 if side == previous_side else 1
                if run_length >= confirm:
                    state = side
            elif half_width == 0.0:
                # No band leaves nothing to sit inside, so there is no state to
                # hold: close == price_ma resolves to flat, as it did before.
                state = 0
                run_length = 0
            streaks[product] = (side, run_length)
        current[product] = state
        states.append(state)
    return states


def _is_finite(value) -> bool:
    try:
        return bool(math.isfinite(value))
    except (TypeError, ValueError):
        return False


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
    # Momentum MAs roll over each product's curve rows (days it is pooled and has
    # a strictly-later secondary).  A pooled day with a main but no secondary has
    # no curve row, so a later window spans that gap rather than resetting on it --
    # see design spec 7 and test_momentum_ma_rolls_over_curve_rows_and_spans_missing_days.
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

    if float(getattr(config, "basis_momentum_weight", 0.0)) > 0.0:
        window = int(config.basis_momentum_window)
        minimum = max(1, int(round(window * float(config.basis_momentum_min_coverage))))
        by_product = signals.groupby("product", sort=False)
        legs = {}
        for leg in ("main_leg_return", "secondary_leg_return"):
            # log1p sums skip NaN, so min_periods counts real observations and
            # nothing else.  Never fillna(0.0) here: padding a young product's
            # window with zero returns hands it a lookback it has not lived
            # through, and lets it be ranked against products that have.
            legs[leg] = by_product[leg].transform(
                lambda values: np.expm1(
                    np.log1p(values).rolling(window, min_periods=minimum).sum()
                )
            )
        signals["basis_momentum"] = (
            legs["main_leg_return"] - legs["secondary_leg_return"]
        )
        signals["bmom_ready"] = (
            _finite_mask(signals["basis_momentum"])
            & signals["main_leg_return"].notna()
            & signals["secondary_leg_return"].notna()
        )

    # Hysteresis trend state, carried forward per product over the curve rows.
    # It is a function of price alone -- close, price_ma and atr -- and never
    # reads position state, so stops and locks cannot perturb it.
    signals["trend_state"] = _trend_states(signals, config)

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

    blending = float(getattr(config, "basis_momentum_weight", 0.0)) > 0.0
    rebalance_dates: set = set()
    struck: dict = {}
    if blending:
        signals["bmom_weight"] = 0.0
        signals["blend_weight"] = 0.0
        rebalance_dates = _rebalance_dates(
            signals["trade_date"],
            config.basis_momentum_rebalance,
        )

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

        if getattr(config, "weighting", "risk_budget") == "rank_linear":
            # Every ready product takes the side of its centred rank, with no
            # sign gate: the top of an all-contango cross-section is still the
            # long leg.  Magnitudes are sized in decision.plan_signal_targets.
            signals.loc[daily.index, "reason"] = "rank_linear"
            weights = rank_linear_weights(ready)
            if blending:
                if trade_date in rebalance_dates:
                    eligible = ready.loc[
                        ready["bmom_ready"].astype(bool)
                    ].sort_values(["basis_momentum", "product"], kind="mergesort")
                    struck = (
                        dict(
                            zip(
                                eligible["product"],
                                rank_linear_weights(
                                    eligible,
                                    "basis_momentum",
                                ).to_numpy(),
                            )
                        )
                        if len(eligible) >= 5
                        else {}
                    )
                # A product that left the pool has no curve row today, so its
                # leg is gone the same day rather than riding the struck rank
                # to month end.  Recentre only when one actually left, so a
                # full cross-section keeps the struck weights bit for bit.
                held = ready["product"].map(struck)
                surviving = held.dropna()
                if len(surviving) != len(struck) and not surviving.empty:
                    held.loc[surviving.index] = surviving - surviving.mean()
                held = held.fillna(0.0)
                share = float(config.basis_momentum_weight)
                weights = (1.0 - share) * weights + share * held
                signals.loc[ready.index, "bmom_weight"] = held.to_numpy()
                signals.loc[ready.index, "blend_weight"] = weights.to_numpy()
            # The blended weight, not the carry rank, decides the side: sizing
            # downstream reads the same number, so a direction taken from the
            # carry leg alone could be sized against itself.
            signals.loc[ready.index, "rank_direction"] = (
                np.sign(weights).astype(int).to_numpy()
            )
        else:
            signals.loc[daily.index, "reason"] = "rank_and_filter"
            selection_count = max(
                1,
                math.floor(len(ready) * config.selection_fraction),
            )
            bottom = ready.head(selection_count)
            top = ready.tail(selection_count)
            # carry_ma > 0 is backwardation (near above far) and carries the
            # roll premium, so the top of the ranking is the long leg;
            # carry_ma < 0 is contango and pays it away, so the bottom is the
            # short leg.
            short_indexes = bottom.loc[bottom["carry_ma"] < 0.0].index
            long_indexes = top.loc[top["carry_ma"] > 0.0].index
            signals.loc[long_indexes, "rank_direction"] = 1
            signals.loc[short_indexes, "rank_direction"] = -1

        for index in daily.index:
            if (
                not signals.at[index, "input_ready"]
                or signals.at[index, "rank_direction"] == 0
            ):
                continue

            direction = signals.at[index, "rank_direction"]
            if not getattr(config, "trend_filter_enabled", True):
                signals.at[index, "strength"] = 1.0
                signals.at[index, "effective_direction"] = direction
                continue

            trend_state = signals.at[index, "trend_state"]
            trend_aligned = trend_state == direction
            trend_opposed = trend_state == -direction
            if trend_aligned:
                strength = 1.0
            elif (
                getattr(config, "allow_trend_opposed", True)
                and trend_opposed
                and signals.at[index, "main_volume"] < signals.at[index, "volume_ma"]
                and signals.at[index, "main_oi"] < signals.at[index, "oi_ma"]
            ):
                strength = 0.5
            else:
                strength = 0.0

            signals.at[index, "strength"] = strength
            if strength > 0.0:
                signals.at[index, "effective_direction"] = direction

    columns = list(_SIGNAL_COLUMNS)
    columns += [name for name in _BLEND_COLUMNS if name in signals.columns]
    return SignalResult(
        signals=signals.loc[:, columns],
        signal_ready_date=signal_ready_date,
    )
