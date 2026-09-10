"""Signal-side chain returns for the basis-momentum leg.

The engine sizes positions from real fills; these are a *signal* input and are
built independently: a forward-only contract chain per product, priced close to
close on the contract held into the day.  A day whose held contract has no bar
yields no return -- this module never synthesises a price, and never differences
two different contracts.
"""

import pandas as pd

_CHAIN_COLUMNS = ("trade_date", "product", "chain_contract")
_RETURN_COLUMNS = ("trade_date", "product", "chain_contract", "leg_return")


def forward_only_chain(picks: pd.DataFrame) -> pd.DataFrame:
    """Roll `picks` forward only: a chain never steps to an earlier delivery.

    `picks` carries one candidate contract per (trade_date, product) with its
    `delivery_yyyymm`.  A pick that delivers no later than the contract already
    held is ignored, so open-interest jitter between two nearby months cannot
    walk the chain backwards and manufacture a return out of the basis.
    """
    if picks.empty:
        return pd.DataFrame(columns=list(_CHAIN_COLUMNS))
    ordered = picks.sort_values(["product", "trade_date"], kind="mergesort")
    rows = []
    for product, sub in ordered.groupby("product", sort=True):
        held_contract = None
        held_delivery = -1
        for trade_date, contract, delivery in zip(
            sub["trade_date"], sub["contract"], sub["delivery_yyyymm"]
        ):
            if pd.isna(contract):
                if held_contract is None:
                    continue
            elif held_contract is None or int(delivery) > held_delivery:
                held_contract, held_delivery = contract, int(delivery)
            rows.append((trade_date, product, held_contract))
    return pd.DataFrame(rows, columns=list(_CHAIN_COLUMNS))


def build_leg_returns(prices: pd.DataFrame, chain: pd.DataFrame) -> pd.DataFrame:
    """Close-to-close return of the contract the chain held into each day.

    The contract priced on T is the one the chain held on T-1, so a roll costs
    nothing here: the day the chain switches still earns yesterday's contract.
    Both closes must exist on the same contract, which drops the day for that
    product when the held contract stopped trading rather than substituting a
    price from the contract that replaced it.
    """
    if chain.empty:
        return pd.DataFrame(columns=list(_RETURN_COLUMNS))
    closes = prices.set_index(["trade_date", "contract"])["close"]
    ordered = chain.sort_values(["product", "trade_date"], kind="mergesort").copy()
    ordered["held"] = ordered.groupby("product", sort=False)["chain_contract"].shift(1)
    ordered["previous_date"] = ordered.groupby("product", sort=False)[
        "trade_date"
    ].shift(1)
    held = ordered.loc[ordered["held"].notna()].copy()
    if held.empty:
        return pd.DataFrame(columns=list(_RETURN_COLUMNS))
    today = pd.MultiIndex.from_arrays([held["trade_date"], held["held"]])
    yesterday = pd.MultiIndex.from_arrays([held["previous_date"], held["held"]])
    held["leg_return"] = (
        closes.reindex(today).to_numpy() / closes.reindex(yesterday).to_numpy() - 1.0
    )
    held = held.dropna(subset=["leg_return"])
    return held.loc[:, list(_RETURN_COLUMNS)].reset_index(drop=True)
