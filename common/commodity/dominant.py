"""商品期货主力合约选择。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import pandas as pd

from common.commodity.universe import canonical_contract
from common.dominant import DominantChoice

__all__ = [
    "DOMINANT_SELECTION_LAG",
    "choose_dominant_commodity",
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
