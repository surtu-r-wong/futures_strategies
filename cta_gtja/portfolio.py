"""Portfolio construction helpers for CTA factor combos."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from cta_gtja.factors import CTAFactor


TRADING_DAYS_PER_YEAR = 252


def factor_weights(factor: CTAFactor, scores: pd.DataFrame) -> pd.DataFrame:
    """Convert one factor's scores into daily symbol weights.

    Returned rows have gross exposure of one where enough data is available.
    Time-series factors use the sign of each symbol's own signal.  Cross-section
    factors are demeaned each day and normalized into a long/short basket.
    """
    if factor.construction == "time_series":
        raw = np.sign(scores).where(scores.notna(), np.nan)
        return normalize_gross(raw)
    if factor.construction == "cross_section":
        demeaned = scores.sub(scores.mean(axis=1, skipna=True), axis=0)
        return normalize_gross(demeaned)
    raise ValueError(f"unknown CTA factor construction: {factor.construction!r}")


def equal_factor_allocations(index: pd.Index, factor_names: list[str]) -> pd.DataFrame:
    if not factor_names:
        return pd.DataFrame(index=index)
    weight = 1.0 / len(factor_names)
    return pd.DataFrame(weight, index=index, columns=factor_names)


def combine_factor_weights(
    weights_by_factor: dict[str, pd.DataFrame],
    allocations: pd.DataFrame,
) -> pd.DataFrame:
    """Combine per-factor symbol weights using date×factor allocations."""
    if not weights_by_factor:
        return pd.DataFrame()
    first = next(iter(weights_by_factor.values()))
    symbols = list(first.columns)
    out = pd.DataFrame(0.0, index=first.index, columns=symbols)
    alloc = allocations.reindex(index=first.index, columns=list(weights_by_factor)).fillna(0.0)
    for name, weights in weights_by_factor.items():
        out = out.add(weights.reindex(index=first.index, columns=symbols).fillna(0.0).mul(alloc[name], axis=0), fill_value=0.0)
    return out


def factor_momentum_allocations(
    factor_returns: pd.DataFrame,
    *,
    lookback_days: int = 60,
    top_n: int = 2,
    max_single_weight: float = 0.50,
) -> pd.DataFrame:
    """Allocate to recently strongest factor sleeves.

    This is the deck's "轮动策略" replication: rank factor sleeves by trailing
    return, allocate equally to the top sleeves, and cap a single sleeve at 50%.
    Before enough history exists, it falls back to equal weights.
    """
    if factor_returns.empty:
        return factor_returns.copy()
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    names = list(factor_returns.columns)
    out = pd.DataFrame(0.0, index=factor_returns.index, columns=names)
    equal = pd.Series(1.0 / len(names), index=names)
    trailing = (1.0 + factor_returns.fillna(0.0)).rolling(lookback_days, min_periods=max(5, lookback_days // 3)).apply(np.prod, raw=True) - 1.0
    for d, row in trailing.iterrows():
        valid = row.dropna()
        if valid.empty:
            alloc = equal.copy()
        else:
            min_names_for_cap = int(math.ceil(1.0 / max_single_weight))
            n_select = min(max(top_n, min_names_for_cap), len(valid))
            winners = valid.sort_values(ascending=False).head(n_select).index
            alloc = pd.Series(0.0, index=names)
            alloc.loc[winners] = 1.0 / len(winners)
        out.loc[d] = cap_and_renormalize(alloc, max_single_weight)
    return out


def blend_allocations(
    smart_beta: pd.DataFrame,
    rotation: pd.DataFrame,
    *,
    smart_beta_weight: float = 0.60,
    max_single_weight: float = 0.50,
) -> pd.DataFrame:
    if not 0.0 <= smart_beta_weight <= 1.0:
        raise ValueError("smart_beta_weight must be in [0, 1]")
    rotation_weight = 1.0 - smart_beta_weight
    cols = list(dict.fromkeys(list(smart_beta.columns) + list(rotation.columns)))
    idx = smart_beta.index.union(rotation.index)
    blended = (
        smart_beta.reindex(idx, columns=cols).fillna(0.0) * smart_beta_weight
        + rotation.reindex(idx, columns=cols).fillna(0.0) * rotation_weight
    )
    return blended.apply(lambda row: cap_and_renormalize(row, max_single_weight), axis=1)


def normalize_gross(weights: pd.DataFrame) -> pd.DataFrame:
    out = weights.astype(float).copy()
    gross = out.abs().sum(axis=1)
    out = out.div(gross.replace(0.0, np.nan), axis=0)
    return out.fillna(0.0)


def cap_and_renormalize(values: pd.Series, cap: float) -> pd.Series:
    if cap <= 0:
        raise ValueError("cap must be positive")
    row = values.astype(float).clip(lower=0.0)
    if row.sum() <= 0:
        return row
    row = row / row.sum()
    # Iterative water-filling cap.  Factor count is tiny, so clarity wins.
    capped = pd.Series(0.0, index=row.index)
    free = row.copy()
    remaining = 1.0
    while not free.empty:
        scaled = free / free.sum() * remaining
        over = scaled > cap
        if not over.any():
            capped.loc[scaled.index] = scaled
            break
        capped.loc[scaled[over].index] = cap
        remaining = 1.0 - capped.sum()
        free = free.loc[~over]
        if remaining <= 1e-12 or free.empty:
            break
    if capped.sum() > 0:
        capped = capped / capped.sum()
    return capped

