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


def _is_market_break(
    old_closes: Mapping[date, float],
    new_closes: Mapping[date, float],
    *,
    chain_gap: bool,
) -> bool:
    """新合约在旧合约还活着的时候根本不存在 —— 两段价格不可比。

    三个条件缺一不可，第三个是为了不把缺价当断代：

    1. 两边都有有效收盘。任一边完全没有价格是缺数据。
    2. 旧合约整段有效收盘都早于新合约的第一天。
    3. **主力链在这里断过**：新主力不是从旧主力的最后一天接手的，中间整段没有
       主力可发 —— 主力必须当天有成交，所以"没有主力"就是"这个品种没在交易"。
       正常换月里链是逐日相接的，此时缺共同收盘只可能是缺数据。

    ⚠️ 第 3 条**不能**写成"旧合约让出主力之后就没有收盘"：归档给已挂牌未成交的
    合约照打结算价。2026-08-31 从生产数据取回的 FU 实况是——`FU1804` 早在停牌前
    就让出了主力，同批老合约（`FU1809`…`FU1812`）逐日零成交却一路打收盘到
    2018-06-28，而 2018 年第一笔 FU 成交出现在新合约上市当天 2018-07-16。
    按"没有收盘"判，全历史唯一一处真断代反而会被判成缺数据、整跑中止。
    """
    if not old_closes or not new_closes:
        return False
    return chain_gap and max(old_closes) < min(new_closes)


def adjustment_factors(
    choices: Sequence[DominantChoice],
    *,
    closes: Mapping[tuple[date, str], float],
) -> pd.DataFrame:
    """沿展期链累乘后复权因子与连续分段。

    列：`product` / `trade_date` / `contract` / `adj_factor` / `continuity_segment`。

    `closes` 是 `(trade_date, contract) -> 收盘价`。正常展期使用判定日之前最近一个
    新旧合约都有收盘的日期算比率。

    **市场断代**：新旧合约的有效收盘日期区间**严格不相交**，且主力链在这里断过
    （新主力不是从旧主力的最后一天接手），就不存在任何可观察的复权比率 —— 这不是
    缺数据，是这个品种被重新挂牌了（燃料油 `FU1804` 末日 2018-03-30，`FU1901` 首日
    2018-07-16，全历史仅此一处）。此时开启下一个连续段：`continuity_segment` 加一，
    因子从 1.0 重新开始。

    区间**有重叠**却找不到共同收盘日，仍然 `roll_close_missing` 硬失败 —— 那更像
    缺数据。悄悄取 1.0 会造出假的无跳空序列。
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
                old = old_closes.get(anchor) if anchor is not None else None
                new = new_closes.get(anchor) if anchor is not None else None
                if old is None or new is None or not new:
                    if _is_market_break(
                        old_closes,
                        new_closes,
                        chain_gap=choice.selected_from != previous.trade_date,
                    ):
                        segment += 1
                        factor = 1.0
                    else:
                        raise ValueError(
                            "roll_close_missing: 展期判定日前没有新旧合约共同收盘价，"
                            "无法算复权因子；"
                            f"not_after={choice.selected_from} {previous.contract!r} "
                            f"-> {choice.contract!r} (anchor={anchor!r}, old={old!r}, "
                            f"new={new!r})"
                        )
                else:
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
    frame = pd.DataFrame.from_records(
        records,
        columns=["product", "trade_date", "contract", "adj_factor", "continuity_segment"],
    )
    if not frame.empty:
        frame["continuity_segment"] = frame["continuity_segment"].astype("int64")
    return frame


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
