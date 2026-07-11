"""Portfolio metrics on a series of period returns.

v0 uses simple geometric compounding. No risk-free rate adjustment, no
factor decomposition, no benchmark relative.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PERIODS_PER_YEAR_MONTHLY = 12


def cumulative_equity(period_returns: pd.Series) -> pd.Series:
    """Equity curve starting at 1.0, compounding period returns geometrically."""
    return (1.0 + period_returns.fillna(0.0)).cumprod()


def max_drawdown(equity: pd.Series) -> float:
    """Worst peak-to-trough decline as a positive fraction (0.20 = 20% drawdown)."""
    if equity.empty:
        return float("nan")
    running_peak = equity.cummax()
    dd = (running_peak - equity) / running_peak
    return float(dd.max())


def summarize(
    period_returns: pd.Series,
    *,
    periods_per_year: int = PERIODS_PER_YEAR_MONTHLY,
    turnover: pd.Series | None = None,
) -> dict[str, float]:
    """One-line summary stats. ``period_returns`` indexed by rebalance date."""
    rets = period_returns.dropna()
    if rets.empty:
        return {
            "ann_return": float("nan"),
            "ann_vol": float("nan"),
            "sharpe": float("nan"),
            "max_drawdown": float("nan"),
            "win_rate": float("nan"),
            "avg_turnover": float("nan"),
            "n_periods": 0,
        }

    equity = cumulative_equity(rets)
    n = len(rets)
    total_return = float(equity.iloc[-1])
    ann_return = total_return ** (periods_per_year / n) - 1
    ann_vol = float(rets.std(ddof=0)) * np.sqrt(periods_per_year)
    sharpe = ann_return / ann_vol if ann_vol > 0 else float("nan")
    win_rate = float((rets > 0).mean())
    mdd = max_drawdown(equity)
    avg_turn = float(turnover.mean()) if turnover is not None and not turnover.empty else float("nan")

    return {
        "ann_return": float(ann_return),
        "ann_vol": float(ann_vol),
        "sharpe": float(sharpe) if not np.isnan(sharpe) else float("nan"),
        "max_drawdown": mdd,
        "win_rate": win_rate,
        "avg_turnover": avg_turn,
        "n_periods": n,
    }


def _ann_return(period_returns: pd.Series, periods_per_year: int) -> float:
    n = len(period_returns)
    if n == 0:
        return float("nan")
    total = float(cumulative_equity(period_returns).iloc[-1])
    return total ** (periods_per_year / n) - 1


def _quarterly_win_rate(port: pd.Series, bench: pd.Series) -> float:
    """Fraction of calendar quarters where the portfolio beat the benchmark."""
    try:
        idx = pd.DatetimeIndex(port.index)
    except (TypeError, ValueError):
        return float("nan")
    p = pd.Series(port.to_numpy(), index=idx)
    b = pd.Series(bench.to_numpy(), index=idx)
    pq = (1.0 + p).resample("QE").prod() - 1.0
    bq = (1.0 + b).resample("QE").prod() - 1.0
    aligned = pd.concat([pq, bq], axis=1, keys=["p", "b"]).dropna()
    if aligned.empty:
        return float("nan")
    return float((aligned["p"] > aligned["b"]).mean())


def summarize_relative(
    port_returns: pd.Series,
    bench_returns: pd.Series,
    *,
    periods_per_year: int = PERIODS_PER_YEAR_MONTHLY,
    turnover: pd.Series | None = None,
) -> dict[str, float]:
    """Benchmark-relative stats for an index-enhancement backtest.

    Aligns the two return series on their common index. ``tracking_error`` is
    the annualized stddev of the period excess returns; ``ann_excess`` is the
    geometric annualized return gap; ``info_ratio = ann_excess / tracking_error``;
    ``rel_max_drawdown`` is the max drawdown of the relative-strength curve
    (port equity / bench equity); ``quarterly_win_rate`` needs a datetime index.
    """
    nan_result = {
        "ann_return": float("nan"),
        "ann_return_bench": float("nan"),
        "ann_excess": float("nan"),
        "tracking_error": float("nan"),
        "info_ratio": float("nan"),
        "rel_max_drawdown": float("nan"),
        "quarterly_win_rate": float("nan"),
        "avg_turnover": float("nan"),
        "n_periods": 0,
    }
    df = pd.concat(
        [port_returns, bench_returns], axis=1, keys=["port", "bench"]
    ).dropna()
    if df.empty:
        return nan_result

    p, b = df["port"], df["bench"]
    excess = p - b
    ann_port = _ann_return(p, periods_per_year)
    ann_bench = _ann_return(b, periods_per_year)
    ann_excess = ann_port - ann_bench
    tracking_error = float(excess.std(ddof=0)) * np.sqrt(periods_per_year)
    info_ratio = ann_excess / tracking_error if tracking_error > 0 else float("nan")
    rel_equity = cumulative_equity(p) / cumulative_equity(b)
    avg_turn = (
        float(turnover.mean())
        if turnover is not None and not turnover.empty
        else float("nan")
    )

    return {
        "ann_return": float(ann_port),
        "ann_return_bench": float(ann_bench),
        "ann_excess": float(ann_excess),
        "tracking_error": float(tracking_error),
        "info_ratio": float(info_ratio),
        "rel_max_drawdown": max_drawdown(rel_equity),
        "quarterly_win_rate": _quarterly_win_rate(p, b),
        "avg_turnover": avg_turn,
        "n_periods": len(p),
    }
