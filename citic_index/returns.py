"""Chain returns, under either return convention, for both the factor and the index.

CITIC states its convention only once in the whole methodology family, in the
trend-enhancement rules:

    r_{i,t} = t 日主力合约最新价 / t 日主力合约「前结算价」 − 1

The denominator is the *previous settlement*, not the previous close.  That
does not cancel down a chain: it accrues close(t-1)/settle(t-1) every day, worth
roughly +1.8pp a year, and it cannot be traded because a settlement price is a
volume-weighted average rather than a price anyone was filled at.  Quoting the
published performance without discounting it overstates what is reachable.

`close_to_close` is the tradeable convention and stays available, because the
difference between the two is exactly the size of that untradeable accrual.

That "+1.8pp" was measured on 025's slow signal.  The accrual is close(t)/settle(t),
which a fast signal has already seen at t and sits on the same side of: on 026's
15-day momentum it is worth +11.8pp a year and grows as the lookback shortens.
026's official series carries none of it (`close_to_close` lands within 0.2pp of
the published level), so whether CITIC's own index really divides by the previous
settlement is open.  `settle_to_settle` -- both ends settlements, no leak --
gives 026 its highest daily correlation (0.936) but a constant drag against the
official level that nothing in the methodology explains.  See
docs/plans/2026-09-15-cicsf026-time-series-momentum-probe.md section 5.

The same function serves the factor's T1 and T2 legs and the index's dominant
leg, so a roll cannot be handled one way in the signal and another in the
return.
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

# basis -> (numerator column, denominator column).  `settle_to_settle` is the
# pure index convention: both ends are settlements, so the close(t)/settle(t)
# gap -- which a fast signal has already seen at t -- never enters day t+1.
BASES = {
    "close_to_prev_settle": ("close", "settle"),
    "close_to_close": ("close", "close"),
    "settle_to_settle": ("settle", "settle"),
}


def _price_lookup(prices: pd.DataFrame, column: str) -> pd.Series:
    return prices.set_index(["trade_date", "contract"])[column]


def chain_returns(
    prices: pd.DataFrame,
    chain: pd.DataFrame,
    *,
    basis: str = "close_to_prev_settle",
    roll_blend: bool = True,
) -> pd.DataFrame:
    """Per product-day return of `chain`, blending both legs on a roll day."""
    if basis not in BASES:
        raise ValueError(f"basis must be one of {tuple(BASES)}, got {basis!r}")
    if chain.empty:
        return pd.DataFrame(columns=list(RETURN_COLUMNS))

    numerator_column, denominator = BASES[basis]
    for column in {numerator_column, denominator}:
        if column not in prices.columns:
            raise ValueError(f"prices carry no {column!r} column for basis {basis!r}")
    closes = _price_lookup(prices, numerator_column)
    bases = _price_lookup(prices, denominator)

    frame = chain.sort_values(["product", "trade_date"], kind="mergesort").copy()
    grouped = frame.groupby("product", sort=False)
    frame["held_contract"] = grouped["chain_contract"].shift(1)
    frame["previous_date"] = grouped["trade_date"].shift(1)
    frame = frame.loc[frame["held_contract"].notna()].copy()
    if frame.empty:
        return pd.DataFrame(columns=list(RETURN_COLUMNS))

    frame["new_contract"] = frame["chain_contract"]
    frame["is_roll"] = frame["new_contract"] != frame["held_contract"]

    def leg(contracts):
        today = pd.MultiIndex.from_arrays([frame["trade_date"], contracts])
        yesterday = pd.MultiIndex.from_arrays([frame["previous_date"], contracts])
        numerator = closes.reindex(today).to_numpy()
        denom = bases.reindex(yesterday).to_numpy()
        return pd.Series(numerator / denom - 1.0, index=frame.index), denom

    old_return, old_base = leg(frame["held_contract"])
    new_return, new_base = leg(frame["new_contract"])

    # 3.5 step 4 weighs the two legs by the new contract's share of contract
    # value.  Both legs are the same product, so the multipliers cancel and the
    # share is a price ratio -- taken at the start of the day, which is what
    # turns two positions' returns into one portfolio return.
    share = pd.Series(new_base / (new_base + old_base), index=frame.index)
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
