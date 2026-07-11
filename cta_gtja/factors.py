"""CTA factors replicated from the product-deck strategy description."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from cta_gtja.data import CTADataSet


@dataclass(frozen=True)
class CTAFactor:
    """Base class for commodity CTA factors.

    ``construction`` controls portfolio conversion:

    - ``time_series``: each symbol gets a long/short signal from its own history.
    - ``cross_section``: scores are demeaned cross-sectionally and normalized
      into a dollar-neutral long/short basket.
    """

    name: str
    construction: str

    def compute(self, data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
        raise NotImplementedError


@dataclass(frozen=True)
class BasisFactor(CTAFactor):
    """Basis factor.

    The deck states: long discounted varieties and short premium varieties.
    Here ``basis_rate`` is interpreted as ``(spot - futures_close) / futures_close``.
    Positive basis therefore means futures are discounted to spot.
    """

    name: str = "basis"
    construction: str = "time_series"

    def compute(self, data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
        basis = data.fundamental_matrix("basis_rate", symbols=symbols)
        if basis.empty:
            spot = data.fundamental_matrix("spot", symbols=symbols)
            close = data.price_matrix("close", symbols=symbols)
            if spot.empty or close.empty:
                return _empty_like(data, symbols)
            basis = spot.reindex_like(close).ffill() / close - 1.0
        return basis.reindex(index=data.dates, columns=symbols).ffill()


@dataclass(frozen=True)
class InventoryFactor(CTAFactor):
    """Inventory factor: long inventory falling faster, short rising faster."""

    lookback_days: int = 20
    name: str = "inventory"
    construction: str = "cross_section"

    def compute(self, data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
        inv = data.fundamental_matrix("inventory", symbols=symbols)
        if inv.empty:
            return _empty_like(data, symbols)
        inv = inv.reindex(index=data.dates, columns=symbols).ffill()
        return -inv.pct_change(self.lookback_days)


@dataclass(frozen=True)
class ProfitFactor(CTAFactor):
    """Profit factor: long profit historically low, short historically high."""

    lookback_days: int = 252
    min_periods: int = 60
    name: str = "profit"
    construction: str = "time_series"

    def compute(self, data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
        profit = data.fundamental_matrix("profit", symbols=symbols)
        if profit.empty:
            return _empty_like(data, symbols)
        profit = profit.reindex(index=data.dates, columns=symbols).ffill()
        z = _rolling_zscore(profit, self.lookback_days, self.min_periods)
        return -z


@dataclass(frozen=True)
class LongRuleMomentumFactor(CTAFactor):
    """Long-horizon rule momentum: short MA above long MA -> long."""

    short_window: int = 20
    long_window: int = 120
    name: str = "long_rule_momentum"
    construction: str = "time_series"

    def compute(self, data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
        close = data.price_matrix("close", symbols=symbols)
        if close.empty:
            return _empty_like(data, symbols)
        short_ma = close.rolling(self.short_window, min_periods=max(3, self.short_window // 2)).mean()
        long_ma = close.rolling(self.long_window, min_periods=max(10, self.long_window // 2)).mean()
        out = pd.DataFrame(np.where(short_ma > long_ma, 1.0, -1.0), index=close.index, columns=close.columns)
        out = out.where(short_ma.notna() & long_ma.notna())
        return out.reindex(index=data.dates, columns=symbols)


@dataclass(frozen=True)
class LongCrossSectionMomentumFactor(CTAFactor):
    """Long-horizon cross-sectional momentum (deck factor 05, GTJAQH013).

    The deck builds this by *regressing each variety's adjusted price on time*
    ("对各品种复权价格时序做回归").  We regress log adjusted price on an evenly
    spaced time index over a trailing window and use the OLS slope as the trend
    strength, which is then ranked cross-sectionally.  Unlike a two-endpoint
    momentum, the slope uses every observation in the window, so it is robust to
    noise at the window's endpoints.
    """

    lookback_days: int = 252
    min_periods: int = 120
    name: str = "long_cross_momentum"
    construction: str = "cross_section"

    def compute(self, data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
        close = data.price_matrix("close", symbols=symbols)
        if close.empty:
            return _empty_like(data, symbols)
        log_price = np.log(close.where(close > 0))
        slope = log_price.rolling(self.lookback_days, min_periods=self.min_periods).apply(
            _ols_slope, raw=True
        )
        return slope.reindex(index=data.dates, columns=symbols)


@dataclass(frozen=True)
class PriceVolumeCorrelationFactor(CTAFactor):
    """Price-volume correlation factor: long low correlation, short high."""

    lookback_days: int = 60
    name: str = "price_volume_corr"
    construction: str = "cross_section"

    def compute(self, data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
        close = data.price_matrix("close", symbols=symbols)
        volume = data.price_matrix("volume", symbols=symbols)
        if close.empty or volume.empty:
            return _empty_like(data, symbols)
        returns = close.pct_change()
        volume_change = volume.replace(0, np.nan).pct_change()
        corr = returns.rolling(self.lookback_days, min_periods=max(10, self.lookback_days // 2)).corr(volume_change)
        return -corr


def default_cta_factors() -> list[CTAFactor]:
    """Six factors used by the deck's CTA factor-combo product."""
    return [
        BasisFactor(),
        InventoryFactor(),
        ProfitFactor(),
        LongRuleMomentumFactor(),
        LongCrossSectionMomentumFactor(),
        PriceVolumeCorrelationFactor(),
    ]


def price_volume_cta_factors() -> list[CTAFactor]:
    """Price/volume-only CTA factors usable before fundamentals are standardized."""
    return [
        LongRuleMomentumFactor(),
        LongCrossSectionMomentumFactor(),
        PriceVolumeCorrelationFactor(),
    ]


def cta_factors_for_set(name: str) -> list[CTAFactor]:
    """Resolve a named CTA factor set."""
    if name == "six_factor":
        return default_cta_factors()
    if name == "price_volume":
        return price_volume_cta_factors()
    raise ValueError(f"unknown CTA factor set: {name!r}")


def _empty_like(data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(np.nan, index=pd.Index(data.dates, name="trade_date"), columns=symbols, dtype=float)


def _rolling_zscore(values: pd.DataFrame, window: int, min_periods: int) -> pd.DataFrame:
    mean = values.rolling(window, min_periods=min_periods).mean()
    std = values.rolling(window, min_periods=min_periods).std(ddof=0)
    return (values - mean) / std.replace(0, np.nan)


def _ols_slope(y: np.ndarray) -> float:
    """OLS slope of ``y`` regressed on an evenly spaced time index 0..n-1.

    Non-finite observations are dropped together with their time index so a
    window with gaps still regresses on the points actually present.
    """
    finite = np.isfinite(y)
    if int(finite.sum()) < 2:
        return np.nan
    x = np.arange(len(y), dtype=float)[finite]
    y = y[finite]
    x_centered = x - x.mean()
    denom = float((x_centered ** 2).sum())
    if denom <= 0.0:
        return np.nan
    return float((x_centered * (y - y.mean())).sum() / denom)

