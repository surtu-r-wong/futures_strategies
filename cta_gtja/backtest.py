"""Daily CTA backtester for replicated factor-combo strategies."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from cta_gtja.data import CTADataSet
from cta_gtja.portfolio import TRADING_DAYS_PER_YEAR
from common.metrics import cumulative_equity, summarize


@dataclass
class CTABacktestResult:
    weights: pd.DataFrame
    period_returns: pd.Series
    turnover: pd.Series
    cost: pd.Series
    equity: pd.Series
    metrics: dict[str, float]
    factor_allocations: pd.DataFrame
    factor_returns: pd.DataFrame
    data_quality: pd.DataFrame = field(default_factory=pd.DataFrame)

    def metrics_frame(self) -> pd.DataFrame:
        return pd.DataFrame([self.metrics])

    def period_returns_frame(self) -> pd.DataFrame:
        return pd.DataFrame({
            "trade_date": self.period_returns.index,
            "net_return": self.period_returns.values,
            "turnover": self.turnover.reindex(self.period_returns.index).values,
            "cost": self.cost.reindex(self.period_returns.index).values,
            "equity": self.equity.reindex(self.period_returns.index).values,
        })

    def weights_frame(self) -> pd.DataFrame:
        return (
            self.weights.stack()
            .rename("weight")
            .reset_index()
            .rename(columns={"level_0": "trade_date", "level_1": "symbol"})
        )


class CTABacktester:
    """Backtest daily target weights on a dominant-contract price series.

    Signals at date ``t`` are executed at next trading day's open and held to
    the following trading day's open, matching the deck's "下一交易日开盘 TWAP"
    assumption as closely as the available OHLC data allows.
    """

    def __init__(
        self,
        data: CTADataSet,
        *,
        cost_bps: float = 1.0,
        target_vol: float | None = None,
        vol_window_days: int = 60,
        max_leverage: float | None = None,
    ):
        self.data = data
        self.cost_bps = float(cost_bps)
        self.target_vol = target_vol
        self.vol_window_days = int(vol_window_days)
        self.max_leverage = max_leverage

    def run(
        self,
        weights: pd.DataFrame,
        *,
        factor_allocations: pd.DataFrame | None = None,
        factor_returns: pd.DataFrame | None = None,
    ) -> CTABacktestResult:
        weights = weights.sort_index().astype(float).fillna(0.0)
        if self.target_vol is not None:
            weights = apply_vol_target(
                weights,
                self._forward_returns(weights.columns.tolist()),
                target_vol=self.target_vol,
                window_days=self.vol_window_days,
                max_leverage=self.max_leverage,
            )
        gross_returns = portfolio_returns(weights, self._forward_returns(weights.columns.tolist()))
        turnover = portfolio_turnover(weights).reindex(gross_returns.index).fillna(0.0)
        cost = turnover * (self.cost_bps / 10000.0)
        net_returns = (gross_returns - cost).dropna()
        turnover = turnover.reindex(net_returns.index)
        cost = cost.reindex(net_returns.index)
        equity = cumulative_equity(net_returns)
        metrics = summarize(net_returns, periods_per_year=TRADING_DAYS_PER_YEAR, turnover=turnover)
        return CTABacktestResult(
            weights=weights,
            period_returns=net_returns,
            turnover=turnover,
            cost=cost,
            equity=equity,
            metrics=metrics,
            factor_allocations=factor_allocations if factor_allocations is not None else pd.DataFrame(index=weights.index),
            factor_returns=factor_returns if factor_returns is not None else pd.DataFrame(index=weights.index),
            data_quality=self.data.data_quality.copy(),
        )

    def _forward_returns(self, symbols: list[str]) -> pd.DataFrame:
        return forward_open_returns(self.data, symbols)


def forward_open_returns(data: CTADataSet, symbols: list[str]) -> pd.DataFrame:
    open_px = data.price_matrix("open", symbols=symbols)
    if open_px.empty:
        open_px = data.price_matrix("close", symbols=symbols)
    # signal date t: enter at open(t+1), exit at open(t+2)
    return open_px.shift(-2) / open_px.shift(-1) - 1.0


def portfolio_returns(weights: pd.DataFrame, asset_returns: pd.DataFrame) -> pd.Series:
    aligned_returns = asset_returns.reindex(index=weights.index, columns=weights.columns)
    return (weights * aligned_returns).sum(axis=1, min_count=1).rename("gross_return")


def portfolio_turnover(weights: pd.DataFrame) -> pd.Series:
    filled = weights.fillna(0.0)
    delta = filled.diff()
    if not filled.empty:
        delta.iloc[0] = filled.iloc[0]
    return delta.abs().sum(axis=1).rename("turnover")


def apply_vol_target(
    weights: pd.DataFrame,
    asset_returns: pd.DataFrame,
    *,
    target_vol: float,
    window_days: int,
    max_leverage: float | None,
) -> pd.DataFrame:
    raw_returns = portfolio_returns(weights, asset_returns).fillna(0.0)
    realized = raw_returns.rolling(window_days, min_periods=max(10, window_days // 3)).std(ddof=0) * np.sqrt(TRADING_DAYS_PER_YEAR)
    scale = target_vol / realized.shift(1)
    scale = scale.replace([np.inf, -np.inf], np.nan).fillna(1.0)
    if max_leverage is not None:
        scale = scale.clip(lower=0.0, upper=max_leverage)
    return weights.mul(scale, axis=0)


def write_cta_outputs(result: CTABacktestResult, output_prefix: str | Path) -> tuple[Path, Path]:
    output_prefix = Path(output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    xlsx_path = output_prefix.with_suffix(".xlsx")
    png_path = output_prefix.with_name(output_prefix.name + "_equity.png")

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        result.metrics_frame().to_excel(writer, sheet_name="metrics", index=False)
        result.period_returns_frame().to_excel(writer, sheet_name="period_returns", index=False)
        result.weights_frame().to_excel(writer, sheet_name="weights", index=False)
        result.factor_allocations.to_excel(writer, sheet_name="factor_allocations")
        result.factor_returns.to_excel(writer, sheet_name="factor_returns")
        if not result.data_quality.empty:
            result.data_quality.to_excel(writer, sheet_name="data_quality", index=False)

    _write_equity_png(result, png_path)
    return xlsx_path, png_path


def _write_equity_png(result: CTABacktestResult, png_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4))
    result.equity.plot(ax=ax, lw=2, color="#1f77b4")
    ax.set_title("CTA factor combo backtest")
    ax.set_ylabel("Cumulative equity")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(png_path, dpi=120)
    plt.close(fig)

