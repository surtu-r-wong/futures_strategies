"""Index accumulation, per CITIC 3.5 steps 4 and 5.

The returns it compounds come from `citic_index.returns.chain_returns`, which
also feeds the factor's legs -- one roll convention for the whole run rather
than one in the signal and another in the return.

    R_{T+1} = sum_p w_p r_p
    MoMI_{T+1} = MoMI_T * (1 + R_{T+1})

with the index set to 1000 on 2010-01-04.

Two things this file is careful about.

The weights struck on T earn T+1's returns.  3.5 is explicit -- "假设 T 日的指数
值为 MoMI_T，计算 T+1 日的指数值" -- and applying a weight to the return of the
day it was computed from is a one-day lookahead that flatters everything and
only shows up as an off-by-one when the replica is lag-scanned against the
official series.

"""

import pandas as pd


INDEX_COLUMNS = ("trade_date", "n_products", "daily_return", "index_value")


def accumulate(
    weights: pd.DataFrame,
    returns: pd.DataFrame,
    *,
    base_date,
    base_value: float = 1000.0,
) -> pd.DataFrame:
    """Compound the weighted cross-section from `base_date` at `base_value`."""
    if weights.empty or returns.empty:
        return pd.DataFrame(columns=list(INDEX_COLUMNS))

    calendar = sorted(set(weights["trade_date"]) | set(returns["trade_date"]))
    next_day = dict(zip(calendar[:-1], calendar[1:]))
    applied = weights.loc[:, ["trade_date", "product", "weight"]].copy()
    applied["earn_on"] = applied["trade_date"].map(next_day)
    applied = applied.dropna(subset=["earn_on"])

    merged = returns.merge(
        applied.loc[:, ["earn_on", "product", "weight"]],
        left_on=["trade_date", "product"],
        right_on=["earn_on", "product"],
        how="inner",
    )
    daily = merged.assign(contribution=merged["weight"] * merged["product_return"])
    per_day = daily.groupby("trade_date").agg(
        n_products=("product", "size"), daily_return=("contribution", "sum")
    )

    # The base date carries no return -- nothing has been struck yet -- so it is
    # absent from both inputs and has to be put on the calendar deliberately.
    # A published index that does not start at its own base point is wrong on
    # its first row, and every level after it is quoted against nothing.
    days = sorted({day for day in calendar if day >= base_date} | {base_date})
    out = pd.DataFrame({"trade_date": days}).join(per_day, on="trade_date")
    out["n_products"] = out["n_products"].fillna(0).astype(int)
    out["daily_return"] = out["daily_return"].fillna(0.0)
    # The base day sets the level; it earns nothing.
    out.loc[out["trade_date"] == base_date, ["n_products", "daily_return"]] = [0, 0.0]
    out["index_value"] = base_value * (1.0 + out["daily_return"]).cumprod()
    return out.loc[:, list(INDEX_COLUMNS)].reset_index(drop=True)
