"""主力合约与后复权连续价 —— 计划 Task 2。

研报附录一在两件事上比 `common.dominant` 的通用规则更严，所以商品线自己实现选取：

1. **双最大**：主力 = 成交量与持仓量**均**达到最大的合约。通用版是「持仓量优先、
   成交量决平」，那是股指线的口径，不是这篇的。
2. **不可逆**：一经切换不再切回，交割月只许非递减。

沿用通用版的一条假设：**滞后一个交易日**（持仓量当日收盘才知道）。双最大不成立时
沿用上一主力（计划 D11）。

## 后复权

```
AdjFactor_i = AdjFactor_{i−1} × Close_{i−1,old} / Close_{i−1,new}
```

基期因子 1，新主力的 O/H/L/C 全乘当期因子。这样算出的收益率就是新合约自己走的
那一段，展期跳空不会被当成行情。

⚠️ 因子会随着展期一路累乘，持续 contango 的品种若干年后连续价会远离真实价格。这
对本策略无害：EMA 穿越的方向、TNR（比值）、`Lev_ATR = 0.005·Close/ATR`（比值）全都
是标度不变的，收益率也是。但**下单手数必须用真实价格**，不能用连续价。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pandas as pd

from common.dominant import DominantChoice
from cta_continuous.universe import canonical_contract

__all__ = [
    "DOMINANT_SELECTION_LAG",
    "adjustment_factors",
    "choose_dominant_commodity",
    "continuous_close",
    "delivery_month",
]

#: 与 `common.dominant.DOMINANT_SELECTION_LAG` 同义，在这里显式重述以便本模块自洽。
DOMINANT_SELECTION_LAG = 1


def delivery_month(symbol: str, trade_date: date) -> tuple[int, int] | None:
    """合约的交割年月；不是可交易月度合约则 ``None``。

    郑商所的三位交割码必须靠 `trade_date` 定年代 —— 不归一就比不出 `TA701` 与
    `TA1701` 是同一个月，「不可逆」判据会被上游那批孪生记录骗到。
    """
    canonical = canonical_contract(symbol, trade_date)
    if canonical is None:
        return None
    delivery = canonical.rsplit(":", 1)[-1]
    return 2000 + int(delivery[:2]), int(delivery[2:])


def choose_dominant_commodity(
    daily: pd.DataFrame,
    *,
    products: Sequence[str],
    lag: int = DOMINANT_SELECTION_LAG,
) -> tuple[DominantChoice, ...]:
    """按研报附录一逐日逐品种选主力：双最大 + 不可逆 + 滞后 ``lag`` 个交易日。

    `daily` 需要 `trade_date` / `symbol` / `oi` / `volume` 四列。
    """
    if type(lag) is not int or lag < 1:
        raise ValueError("dominant_lag: lag 必须是 >= 1 的整数")
    missing = {"trade_date", "symbol", "oi", "volume"} - set(daily.columns)
    if missing:
        raise ValueError(f"dominant_columns: 缺列 {sorted(missing)}")

    frame = daily.copy()
    canonical = [
        canonical_contract(symbol, trade_date)
        for symbol, trade_date in zip(frame["symbol"], frame["trade_date"])
    ]
    frame["contract_key"] = canonical
    frame["product"] = [value.split(":")[1] if value else None for value in canonical]
    frame = frame.loc[frame["product"].notna()]

    # 2015--2017 年 CZCE 同一张合约会同时以三位、四位交割码出现。它们若参与
    # `_both_max`，同一个最大值会被误判成两个 winner；先按规范合约键去重。
    twins = frame.groupby(["contract_key", "trade_date"], sort=False)[
        ["oi", "volume"]
    ].nunique()
    disagreement = twins.loc[(twins["oi"] > 1) | (twins["volume"] > 1)]
    if len(disagreement):
        contract_key, trade_date = disagreement.index[0]
        raise ValueError(
            "dominant_duplicate_disagreement: 同一张合约的孪生日线量仓不一致；"
            f"{trade_date} {contract_key}"
        )
    frame = frame.drop_duplicates(
        subset=["contract_key", "trade_date"], keep="first"
    )

    sessions = sorted(set(frame["trade_date"]))
    chosen: list[DominantChoice] = []
    for product in products:
        rows = frame.loc[frame["product"] == product]
        if rows.empty:
            raise ValueError(f"dominant_missing_product: 日线里没有 {product!r}")
        held_key: str | None = None
        held_month: tuple[int, int] | None = None
        for index in range(lag, len(sessions)):
            trade_date = sessions[index]
            source_date = sessions[index - lag]
            pool = rows.loc[rows["trade_date"] == source_date]
            if pool.empty:
                continue

            candidate = _both_max(pool, source_date)
            if candidate is not None:
                month = delivery_month(candidate, source_date)
                if held_month is None or month >= held_month:
                    held_key = canonical_contract(candidate, source_date)
                    held_month = month
            if held_key is None:
                continue

            row = pool.loc[pool["contract_key"] == held_key]
            # D11 的「沿用」只适用于旧主力仍在当日合约池的情形。已经退市/缺档的
            # 合约不能被伪造成 oi=volume=0 的可交易主力；等下一张双最大出现再恢复。
            if row.empty:
                continue
            chosen.append(
                DominantChoice(
                    trade_date=trade_date,
                    product=product,
                    contract=str(row["symbol"].iloc[0]),
                    oi=int(row["oi"].iloc[0]),
                    volume=int(row["volume"].iloc[0]),
                    selected_from=source_date,
                )
            )
    return tuple(sorted(chosen, key=lambda c: (c.trade_date, c.product)))


def _both_max(pool: pd.DataFrame, source_date: date) -> str | None:
    """成交量与持仓量**同时**最大的那张合约；没有则 ``None``（研报未写，见 D11）。"""
    top_oi = pool["oi"].max()
    top_volume = pool["volume"].max()
    winners = pool.loc[(pool["oi"] == top_oi) & (pool["volume"] == top_volume)]
    if len(winners) != 1:
        return None
    return str(winners["symbol"].iloc[0])


def adjustment_factors(
    choices: Sequence[DominantChoice],
    *,
    closes: Mapping[tuple[date, str], float],
) -> pd.DataFrame:
    """沿展期链累乘后复权因子，并标记行情连续段。

    `closes` 是 `(trade_date, contract) -> 收盘价`。正常展期使用判定日前一交易日的
    新旧收盘；若旧主力退市造成主力链空档，则使用判定日之前最近一个新旧合约都有
    收盘的日期。只有旧合约行情严格早于新合约行情时才开始新连续段；其他找不到共同
    日期的情形仍报错 —— 悄悄取 1.0 会造出假的无跳空序列。
    """
    ordered = sorted(choices, key=lambda c: (c.product, c.trade_date))
    closes_by_contract: dict[str, dict[date, float]] = {}
    for (trade_date, contract), close in closes.items():
        key = canonical_contract(contract, trade_date) or str(contract)
        values = closes_by_contract.setdefault(key, {})
        value = float(close)
        previous_value = values.get(trade_date)
        if previous_value is not None and previous_value != value:
            raise ValueError(
                "roll_close_alias_disagreement: 同一张合约的别名收盘价不一致；"
                f"{trade_date} {key} ({previous_value!r}, {value!r})"
            )
        values[trade_date] = value

    records: list[dict[str, object]] = []
    factor = 1.0
    segment = 0
    previous: DominantChoice | None = None
    for choice in ordered:
        if previous is None or previous.product != choice.product:
            factor = 1.0
            segment = 0
        else:
            old_key = canonical_contract(previous.contract, previous.trade_date) or previous.contract
            new_key = canonical_contract(choice.contract, choice.trade_date) or choice.contract
            if old_key != new_key:
                old_closes = closes_by_contract.get(old_key, {})
                new_closes = closes_by_contract.get(new_key, {})
                common_dates = old_closes.keys() & new_closes.keys()
                eligible_dates = [
                    value for value in common_dates if value <= choice.selected_from
                ]
                anchor = max(eligible_dates) if eligible_dates else None
                if anchor is None:
                    old_dates = set(old_closes)
                    new_dates = set(new_closes)
                    if (
                        old_dates
                        and new_dates
                        and max(old_dates) < min(new_dates)
                    ):
                        segment += 1
                        factor = 1.0
                    else:
                        raise ValueError(
                            "roll_close_missing: 展期判定日前没有新旧合约共同收盘价，"
                            "无法算复权因子；"
                            f"not_after={choice.selected_from} {previous.contract!r} "
                            f"-> {choice.contract!r} (anchor={anchor!r}, old=None, "
                            "new=None)"
                        )
                else:
                    old = old_closes.get(anchor)
                    new = new_closes.get(anchor)
                    if old is None or new is None or not new:
                        raise ValueError(
                            "roll_close_missing: 展期判定日前没有新旧合约共同收盘价，"
                            "无法算复权因子；"
                            f"not_after={choice.selected_from} {previous.contract!r} "
                            f"-> {choice.contract!r} (anchor={anchor!r}, old={old!r}, "
                            f"new={new!r})"
                        )
                    factor *= float(old) / float(new)
        records.append(
            {
                "product": choice.product,
                "trade_date": choice.trade_date,
                "contract": choice.contract,
                "adj_factor": factor,
                "continuity_segment": segment,
            }
        )
        previous = choice
    return pd.DataFrame.from_records(
        records,
        columns=[
            "product",
            "trade_date",
            "contract",
            "adj_factor",
            "continuity_segment",
        ],
    )


def continuous_close(
    factors: pd.DataFrame, *, closes: Mapping[tuple[date, str], float]
) -> pd.DataFrame:
    """把复权因子铺到收盘价上，得到连续序列。列：`product` / `trade_date` / `close`。"""
    values = []
    for product, trade_date, contract, factor in factors.loc[
        :, ["product", "trade_date", "contract", "adj_factor"]
    ].itertuples(index=False):
        raw = closes.get((trade_date, contract))
        if raw is None:
            raise ValueError(
                f"continuous_close_missing: {trade_date} {contract!r} 没有收盘价"
            )
        values.append(
            {"product": product, "trade_date": trade_date, "close": float(raw) * factor}
        )
    return pd.DataFrame.from_records(values, columns=["product", "trade_date", "close"])
