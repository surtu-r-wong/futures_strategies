"""Measure a replica against the series CITIC publishes.

The headline number is the correlation of daily returns, but it is never read
on its own.  The 025 cross-validation once showed a daily correlation of 0.105
against a replica that was in fact the same strategy one day out of step --
lagging it one day read 0.87.  So `lag_scan` runs first and its result is part
of the verdict: a low correlation at lag zero with a high one at lag +-1 means
an alignment bug, not a specification gap.
"""

import numpy as np
import pandas as pd

from common.metrics import summarize


def align(replica: pd.DataFrame, official: pd.DataFrame, *, code: str) -> pd.DataFrame:
    """Both series on the days they share, as levels and as daily returns."""
    theirs = official.loc[official["index_code"] == code, ["trade_date", "close"]]
    if theirs.empty:
        raise ValueError(f"no official rows for {code}")
    mine = replica.loc[:, ["trade_date", "index_value"]]
    for frame in (mine, theirs):
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date

    merged = mine.merge(theirs, on="trade_date", how="inner", validate="one_to_one")
    merged = merged.sort_values("trade_date").reset_index(drop=True)
    merged = merged.rename(columns={"index_value": "replica", "close": "official"})
    merged["replica_return"] = merged["replica"].pct_change()
    merged["official_return"] = merged["official"].pct_change()
    return merged


def lag_scan(aligned: pd.DataFrame, *, lags=range(-3, 4)) -> pd.DataFrame:
    """corr(replica_t, official_{t-k}) for each k, so a one-day slip is visible."""
    rows = []
    for lag in lags:
        shifted = aligned["official_return"].shift(lag)
        pair = pd.concat([aligned["replica_return"], shifted], axis=1).dropna()
        rows.append(
            {
                "lag": lag,
                "n": len(pair),
                "correlation": (
                    float(pair.iloc[:, 0].corr(pair.iloc[:, 1])) if len(pair) > 2 else float("nan")
                ),
            }
        )
    return pd.DataFrame(rows)


def _rank_correlation(left: pd.Series, right: pd.Series) -> float:
    pair = pd.concat([left, right], axis=1).dropna()
    if len(pair) < 3:
        return float("nan")
    return float(pair.iloc[:, 0].rank().corr(pair.iloc[:, 1].rank()))


def performance(returns: pd.Series) -> dict:
    stats = summarize(returns.dropna(), periods_per_year=252)
    return {
        "ann_return": stats["ann_return"],
        "ann_vol": stats["ann_vol"],
        "sharpe": stats["sharpe"],
        "max_drawdown": stats["max_drawdown"],
        "days": stats["n_periods"],
    }


def yearly(aligned: pd.DataFrame) -> pd.DataFrame:
    """Calendar-year returns side by side, to see which years the gap lives in."""
    frame = aligned.copy()
    frame["year"] = pd.to_datetime(frame["trade_date"]).dt.year
    rows = []
    for year, group in frame.groupby("year"):
        rows.append(
            {
                "year": int(year),
                "days": len(group),
                "replica": float((1.0 + group["replica_return"].fillna(0.0)).prod() - 1.0),
                "official": float((1.0 + group["official_return"].fillna(0.0)).prod() - 1.0),
                # A year with one shared day has no correlation to report, and
                # asking numpy for one divides by a zero standard deviation.
                "correlation": (
                    float(group["replica_return"].corr(group["official_return"]))
                    if group["replica_return"].notna().sum() > 2
                    else float("nan")
                ),
            }
        )
    out = pd.DataFrame(rows)
    out["gap"] = out["replica"] - out["official"]
    return out


def compare(replica: pd.DataFrame, official: pd.DataFrame, *, code: str) -> dict:
    """Everything needed to judge one replica, in one call."""
    aligned = align(replica, official, code=code)
    scan = lag_scan(aligned)
    at_zero = scan.loc[scan["lag"] == 0, "correlation"].item()
    best = scan.loc[scan["correlation"].abs().idxmax()]
    return {
        "code": code,
        "aligned": aligned,
        "days": len(aligned),
        "lag_scan": scan,
        "correlation": at_zero,
        "best_lag": int(best["lag"]),
        "best_correlation": float(best["correlation"]),
        "rank_correlation": _rank_correlation(
            aligned["replica_return"], aligned["official_return"]
        ),
        "level_correlation": float(aligned["replica"].corr(aligned["official"])),
        "replica_performance": performance(aligned["replica_return"]),
        "official_performance": performance(aligned["official_return"]),
        "yearly": yearly(aligned),
    }


def render(result: dict) -> str:
    """A single readable block; the lag scan comes first on purpose."""
    lines = [
        f"=== {result['code']}  {result['days']} shared days ===",
        "",
        "lag scan  corr(replica_t, official_{t-k}):",
        result["lag_scan"].to_string(index=False, float_format=lambda v: f"{v:,.3f}"),
        "",
    ]
    if abs(result["best_correlation"]) > abs(result["correlation"]) + 0.05:
        lines.append(
            f"!! the strongest correlation is at lag {result['best_lag']}"
            f" ({result['best_correlation']:.3f} against {result['correlation']:.3f}"
            " at zero) -- alignment, not specification"
        )
        lines.append("")
    lines += [
        f"daily return correlation : {result['correlation']:.3f}",
        f"rank correlation         : {result['rank_correlation']:.3f}",
        f"level correlation        : {result['level_correlation']:.3f}",
        "",
        f"{'':<10}{'ann':>9}{'vol':>9}{'sharpe':>9}{'maxdd':>9}",
    ]
    for name, key in (("replica", "replica_performance"), ("official", "official_performance")):
        stats = result[key]
        lines.append(
            f"{name:<10}{stats['ann_return']:>9.2%}{stats['ann_vol']:>9.2%}"
            f"{stats['sharpe']:>9.2f}{stats['max_drawdown']:>9.2%}"
        )
    lines += [
        "",
        "by calendar year:",
        result["yearly"].to_string(
            index=False,
            formatters={
                "replica": lambda v: f"{v:,.2%}",
                "official": lambda v: f"{v:,.2%}",
                "gap": lambda v: f"{v:,.2%}",
                "correlation": lambda v: f"{v:,.3f}",
            },
        ),
    ]
    return "\n".join(lines)
