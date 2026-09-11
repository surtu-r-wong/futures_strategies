"""Product pool, per CITIC 3.2.

Three gates: a twenty-day mean of open-interest value at or above two billion
yuan, three months of listing, and membership of the thirty-seven products the
methodology names.  The last is a switch rather than a constant because the
production carry config excludes the metals that list names, and the
attribution task has to be able to price that difference.
"""

import pandas as pd


# 3.2: "指数测试考虑历史流动性较高的 37 个品种".  Order follows the document.
NAMED_37 = frozenset(
    {
        "AL",  # 沪铝
        "AU",  # 沪金
        "AG",  # 沪银
        "CU",  # 沪铜
        "ZN",  # 沪锌
        "NI",  # 沪镍
        "RB",  # 螺纹钢
        "HC",  # 热轧卷板
        "J",   # 焦炭
        "JM",  # 焦煤
        "I",   # 铁矿石
        "FG",  # 玻璃
        "FU",  # 燃油
        "SC",  # 原油
        "V",   # PVC
        "RU",  # 橡胶
        "BU",  # 沥青
        "SP",  # 纸浆
        "PG",  # 液化石油气
        "C",   # 玉米
        "L",   # 塑料
        "M",   # 豆粕
        "P",   # 棕榈油
        "Y",   # 豆油
        "JD",  # 鸡蛋
        "PP",  # 聚丙烯
        "CS",  # 玉米淀粉
        "EG",  # 乙二醇
        "CF",  # 郑棉
        "OI",  # 菜油
        "SR",  # 白糖
        "TA",  # PTA
        "MA",  # 甲醇
        "RM",  # 菜粕
        "ZC",  # 动力煤
        "SM",  # 锰硅
        "AP",  # 苹果
    }
)


POOL_COLUMNS = (
    "trade_date",
    "product",
    "multiplier",
    "open_interest_value",
    "liquidity_mean",
    "listed_days",
    "in_pool",
    "reason",
)


def _daily_multipliers(prices: pd.DataFrame) -> pd.Series:
    """Expanding median of turnover / (volume * close), per product.

    The daily bars carry no contract-size metadata before 2025, so the size is
    inferred from the bars themselves.  Taking the median across a day's
    contracts shrugs off an odd bar; expanding it rather than taking the whole
    sample keeps the estimate free of anything the run has not reached yet.
    """
    traded = prices.loc[(prices["volume"] > 0) & (prices["turnover"] > 0)].copy()
    traded["ratio"] = traded["turnover"] / (traded["volume"] * traded["close"])
    daily = traded.groupby(["product", "trade_date"], sort=True)["ratio"].median()
    return daily.groupby(level="product").expanding().median().droplevel(0)


def pool_membership(
    prices: pd.DataFrame,
    *,
    liquidity_window: int = 20,
    threshold: float = 2e9,
    min_listing_calendar_days: int = 90,
    restrict_to_named: bool = True,
) -> pd.DataFrame:
    """One row per product-day saying whether the product is in the pool, and why.

    `prices` carries one row per contract-day with trade_date, product,
    contract, close, volume, oi and turnover.
    """
    if prices.empty:
        return pd.DataFrame(columns=list(POOL_COLUMNS))

    multipliers = _daily_multipliers(prices)

    frame = prices.groupby(["product", "trade_date"], sort=True).apply(
        lambda day: (day["oi"] * day["close"]).sum(), include_groups=False
    )
    frame = frame.rename("oi_value").to_frame()
    frame["multiplier"] = multipliers
    frame["open_interest_value"] = frame["oi_value"] * frame["multiplier"]

    frame = frame.reset_index()
    frame = frame.sort_values(["product", "trade_date"], kind="mergesort")
    frame["liquidity_mean"] = (
        frame.groupby("product", sort=False)["open_interest_value"]
        .rolling(liquidity_window, min_periods=liquidity_window)
        .mean()
        .reset_index(level=0, drop=True)
    )
    listed_from = frame.groupby("product", sort=False)["trade_date"].transform("min")
    frame["listed_days"] = [
        (day - start).days for day, start in zip(frame["trade_date"], listed_from)
    ]

    named_out = restrict_to_named & ~frame["product"].isin(NAMED_37)
    too_young = frame["listed_days"] < min_listing_calendar_days
    window_short = frame["liquidity_mean"].isna()
    illiquid = frame["liquidity_mean"] < threshold

    frame["reason"] = "in_pool"
    frame.loc[illiquid, "reason"] = "below_liquidity_threshold"
    frame.loc[window_short, "reason"] = "liquidity_window_not_ready"
    frame.loc[too_young, "reason"] = "insufficient_listing"
    frame.loc[named_out, "reason"] = "not_in_named_universe"
    frame["in_pool"] = frame["reason"].eq("in_pool")

    return frame.loc[:, list(POOL_COLUMNS)].reset_index(drop=True)
