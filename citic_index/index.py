"""Index accumulation, per CITIC 3.5 steps 4 and 5.

    R_{T+1} = sum_p w_p r_p
    MoMI_{T+1} = MoMI_T * (1 + R_{T+1})

with the index set to 1000 on 2010-01-04.

Two things this file is careful about.

The weights struck on T earn T+1's returns.  3.5 is explicit -- "假设 T 日的指数
值为 MoMI_T，计算 T+1 日的指数值" -- and applying a weight to the return of the
day it was computed from is a one-day lookahead that flatters everything and
only shows up as an off-by-one when the replica is lag-scanned against the
official series.

On the day the chain rolls, 3.5 step 4 blends the two contracts:
`r = w_bar*r_new + (1-w_bar)*r_old`, w_bar being the new contract's share of
contract value.  Both contracts belong to the same product, so their multipliers
cancel and the share is the previous close of the new over the sum of the two
previous closes -- value at the start of the day, which is what turns two
positions' returns into one portfolio return.  The production chain instead
earns whatever it held into the day, which is deviation 7 and what
`roll_blend=False` reproduces.
"""

import pandas as pd


RETURN_COLUMNS = (
    "trade_date",
    "product",
    "held_contract",
    "new_contract",
    "is_roll",
    "value_share_new",
    "product_return",
)

INDEX_COLUMNS = ("trade_date", "n_products", "daily_return", "index_value")


def _close_lookup(prices: pd.DataFrame) -> pd.Series:
    return prices.set_index(["trade_date", "contract"])["close"]


def _leg_return(closes: pd.Series, days, previous_days, contracts) -> pd.Series:
    today = pd.MultiIndex.from_arrays([days, contracts])
    yesterday = pd.MultiIndex.from_arrays([previous_days, contracts])
    return pd.Series(
        closes.reindex(today).to_numpy() / closes.reindex(yesterday).to_numpy() - 1.0,
        index=days.index,
    )


def blended_returns(
    prices: pd.DataFrame,
    chain: pd.DataFrame,
    *,
    roll_blend: bool = True,
) -> pd.DataFrame:
    """Per product-day return of the chain, blending the two legs on a roll."""
    if chain.empty:
        return pd.DataFrame(columns=list(RETURN_COLUMNS))

    closes = _close_lookup(prices)
    frame = chain.sort_values(["product", "trade_date"], kind="mergesort").copy()
    grouped = frame.groupby("product", sort=False)
    frame["held_contract"] = grouped["chain_contract"].shift(1)
    frame["previous_date"] = grouped["trade_date"].shift(1)
    frame = frame.loc[frame["held_contract"].notna()].copy()
    if frame.empty:
        return pd.DataFrame(columns=list(RETURN_COLUMNS))

    frame["new_contract"] = frame["chain_contract"]
    frame["is_roll"] = frame["new_contract"] != frame["held_contract"]

    old_return = _leg_return(
        closes, frame["trade_date"], frame["previous_date"], frame["held_contract"]
    )
    new_return = _leg_return(
        closes, frame["trade_date"], frame["previous_date"], frame["new_contract"]
    )

    old_value = closes.reindex(
        pd.MultiIndex.from_arrays([frame["previous_date"], frame["held_contract"]])
    ).to_numpy()
    new_value = closes.reindex(
        pd.MultiIndex.from_arrays([frame["previous_date"], frame["new_contract"]])
    ).to_numpy()
    share = pd.Series(new_value / (new_value + old_value), index=frame.index)
    # A leg that did not trade yesterday has no return and no value to weigh, so
    # it cannot take part in the blend.
    share = share.where(frame["is_roll"] & new_return.notna())

    if roll_blend:
        blended = share * new_return + (1.0 - share) * old_return
        frame["product_return"] = blended.where(share.notna(), old_return)
    else:
        frame["product_return"] = old_return
    frame["value_share_new"] = share

    frame = frame.dropna(subset=["product_return"])
    frame = frame.sort_values(["trade_date", "product"], kind="mergesort")
    return frame.loc[:, list(RETURN_COLUMNS)].reset_index(drop=True)


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

    days = [day for day in calendar if day >= base_date]
    out = pd.DataFrame({"trade_date": days}).join(per_day, on="trade_date")
    out["n_products"] = out["n_products"].fillna(0).astype(int)
    out["daily_return"] = out["daily_return"].fillna(0.0)
    # The base day sets the level; it earns nothing.
    out.loc[out["trade_date"] == base_date, ["n_products", "daily_return"]] = [0, 0.0]
    out["index_value"] = base_value * (1.0 + out["daily_return"]).cumprod()
    return out.loc[:, list(INDEX_COLUMNS)].reset_index(drop=True)
