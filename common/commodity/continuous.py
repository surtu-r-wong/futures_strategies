"""商品期货后复权连续价。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pandas as pd

from common.commodity.universe import canonical_contract
from common.dominant import DominantChoice

__all__ = [
    "adjustment_factors",
    "continuous_close",
]


def adjustment_factors(
    choices: Sequence[DominantChoice],
    *,
    closes: Mapping[tuple[date, str], float],
) -> pd.DataFrame:
    """沿展期链累乘后复权因子。列：`product` / `trade_date` / `contract` / `adj_factor`。

    `closes` 是 `(trade_date, contract) -> 收盘价`。正常展期使用判定日前一交易日的
    新旧收盘；若旧主力退市造成主力链空档，则使用判定日之前最近一个新旧合约都有
    收盘的日期。找不到共同日期仍报错 —— 悄悄取 1.0 会造出假的无跳空序列。
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
    previous: DominantChoice | None = None
    for choice in ordered:
        if previous is None or previous.product != choice.product:
            factor = 1.0
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
                old = old_closes.get(anchor) if anchor is not None else None
                new = new_closes.get(anchor) if anchor is not None else None
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
            }
        )
        previous = choice
    return pd.DataFrame.from_records(
        records, columns=["product", "trade_date", "contract", "adj_factor"]
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
