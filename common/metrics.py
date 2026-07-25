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
    # A period return <= -100% drives cumulative equity <= 0; a negative base to
    # a fractional power is complex, so guard to NaN rather than crash on float().
    ann_return = (
        total_return ** (periods_per_year / n) - 1
        if total_return > 0.0
        else float("nan")
    )
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
