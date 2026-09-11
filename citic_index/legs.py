"""Three-leg selection, per CITIC 3.5 step 1.

The dominant contract is the one holding the most open interest.  T1 is the
highest-open-interest contract delivering *before* it -- "主力合约持仓之前的持仓
最大的合约，如果没有就是主力" -- and T2 is the highest-open-interest contract
delivering after it.  Both legs go by open interest, not by nearness: the
nearest later month loses to a further one that is more heavily held.

3.1 words T1 differently, as the dominant itself.  3.5 is the operative text --
it is the step list the index is computed from, and it matches the near leg in
the term-structure methodology -- so 3.5 wins.  `t1_leg="main"` reproduces the
other reading, which is what the shipped basis-momentum leg does; it is a
switch so the attribution task can price the difference.
"""

import pandas as pd

# The same calendar arithmetic the carry curve uses.  Imported rather than
# copied: two spellings of "months between two delivery months" is one more
# than this repo needs.
from cta_carry.curve import _month_gap


LEG_COLUMNS = (
    "trade_date",
    "product",
    "main_contract",
    "main_delivery_yyyymm",
    "main_close",
    "t1_contract",
    "t1_delivery_yyyymm",
    "t1_close",
    "t2_contract",
    "t2_delivery_yyyymm",
    "t2_close",
    "month_gap",
)

_T1_LEGS = ("near_dominant", "main")
_KEY = ["trade_date", "product"]


def _highest_oi_first(prices: pd.DataFrame) -> pd.DataFrame:
    """Open interest descending, contract code ascending to break ties."""
    return prices.sort_values(
        ["trade_date", "product", "oi", "contract"],
        ascending=[True, True, False, True],
        kind="mergesort",
    )


def _rename_leg(frame: pd.DataFrame, prefix: str) -> pd.DataFrame:
    return frame.loc[
        :, _KEY + ["contract", "delivery_yyyymm", "close"]
    ].rename(
        columns={
            "contract": f"{prefix}_contract",
            "delivery_yyyymm": f"{prefix}_delivery_yyyymm",
            "close": f"{prefix}_close",
        }
    )


def select_legs(prices: pd.DataFrame, *, t1_leg: str = "near_dominant") -> pd.DataFrame:
    """One row per product-day naming the dominant, T1 and T2 contracts.

    A product-day with nothing delivering after the dominant has no term
    structure to measure and is dropped rather than filled.
    """
    if t1_leg not in _T1_LEGS:
        raise ValueError(f"t1_leg must be one of {_T1_LEGS}, got {t1_leg!r}")
    if prices.empty:
        return pd.DataFrame(columns=list(LEG_COLUMNS))

    ordered = _highest_oi_first(prices)
    main = ordered.drop_duplicates(_KEY, keep="first")
    legs = _rename_leg(main, "main")

    # `ordered` is already open-interest descending, so the first surviving row
    # of each group is the most heavily held one on that side of the dominant.
    with_main = ordered.merge(
        main.loc[:, _KEY + ["delivery_yyyymm"]].rename(
            columns={"delivery_yyyymm": "_main_delivery"}
        ),
        on=_KEY,
        how="left",
    )
    later = with_main.loc[
        with_main["delivery_yyyymm"] > with_main["_main_delivery"]
    ].drop_duplicates(_KEY, keep="first")
    legs = legs.merge(_rename_leg(later, "t2"), on=_KEY, how="inner")

    if t1_leg == "near_dominant":
        earlier = with_main.loc[
            with_main["delivery_yyyymm"] < with_main["_main_delivery"]
        ].drop_duplicates(_KEY, keep="first")
        legs = legs.merge(_rename_leg(earlier, "t1"), on=_KEY, how="left")
        missing = legs["t1_contract"].isna()
        for target, source in (
            ("t1_contract", "main_contract"),
            ("t1_delivery_yyyymm", "main_delivery_yyyymm"),
            ("t1_close", "main_close"),
        ):
            legs.loc[missing, target] = legs.loc[missing, source]
    else:
        for target, source in (
            ("t1_contract", "main_contract"),
            ("t1_delivery_yyyymm", "main_delivery_yyyymm"),
            ("t1_close", "main_close"),
        ):
            legs[target] = legs[source]

    legs["t1_delivery_yyyymm"] = legs["t1_delivery_yyyymm"].astype(int)
    legs["month_gap"] = [
        _month_gap(near, far)
        for near, far in zip(legs["t1_delivery_yyyymm"], legs["t2_delivery_yyyymm"])
    ]
    legs = legs.sort_values(_KEY, kind="mergesort")
    return legs.loc[:, list(LEG_COLUMNS)].reset_index(drop=True)
