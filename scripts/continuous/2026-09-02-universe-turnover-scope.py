"""宇宙口径的第二种读法：「日均成交额」只算主力那一张合约，池子会窄多少（诊断，不改口径）。

研报 §5.1 原文（2026-09-02 拿 PDF 第 18 页**原图**核过，不是文字提取）：

    投资标的：过去半年中日均成交额超过 50 亿元的商品期货全品种的主力合约。

门槛 50 亿与「过去半年」都是研报给的。研报**没写**的是「日均成交额」算在谁头上：

- **读法 A（我们的 Task 1）**：算在**品种**上 —— 该品种当日全部合约成交额之和。
- **读法 B（本脚本）**：算在**主力合约**那一张上 —— 研报只交易主力，同一句话读得通。

B ⊆ A（主力成交额 ≤ 品种成交额），所以 B 的宇宙是 A 的子集，**面板不必重建**。
本脚本只量三件事：两种读法逐月的品种数、主力成交额占品种的比例、被 B 剔掉的是谁。
**不跑回测、不下策略结论。**

## 孪生记录：491 组只差 1.00 元

`product_daily_turnover` 的守卫要求孪生记录成交额**严格相等**，否则硬失败
（"上游缺陷换了形态"）。2026-09-02 实测：2015-09-21..2017-01-16 有 491 组
郑商所三位/四位孪生的 turnover **恰好差 1.00 元**（相对差 ≤2.4e-6，10 个品种，
无 NULL），是两源对元的舍入差。本脚本**不动生产口径**，而是在喂进去之前
把这类差额 ≤1 元的孪生对齐成同一个值，并把改了多少条打出来；超出容差的仍然让守卫炸。
"""

from __future__ import annotations

import resource
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.config import load_config, resolve_settings_path  # noqa: E402
from common.db import get_connection, pg_config_from  # noqa: E402
from cta_continuous.continuous import choose_dominant_commodity  # noqa: E402
from cta_continuous.scope import DAILY_FROM, next_month  # noqa: E402
from cta_continuous.universe import (  # noqa: E402
    canonical_contract,
    product_daily_turnover,
    universe_for_month,
)

RAW = Path("output/continuous/futures_daily_scope_20260902.csv")
OUT_TRACE = Path("output/continuous/2026-09-02-universe-scope-trace.csv")
OUT_SHARE = Path("output/continuous/2026-09-02-dominant-share.csv")

FIRST_MONTH = date(2011, 1, 1)
LAST_MONTH = date(2026, 1, 1)
DAILY_UNTIL = date(2026, 2, 1)

#: 孪生成交额允许的对元舍入差。超过就让生产守卫硬失败。
TWIN_TOLERANCE_YUAN = 1.0


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def fetch() -> pd.DataFrame:
    if RAW.exists():
        _log(f"复用 {RAW}")
    else:
        cfg = load_config(resolve_settings_path())
        sql = (
            "SELECT symbol, trade_date, oi, volume, turnover "
            "FROM public.futures_daily "
            f"WHERE trade_date >= DATE '{DAILY_FROM}' AND trade_date < DATE '{DAILY_UNTIL}' "
            "AND oi IS NOT NULL AND volume IS NOT NULL"
        )
        RAW.parent.mkdir(parents=True, exist_ok=True)
        with get_connection(pg_config_from(cfg)) as conn, conn.cursor() as cur:
            cur.execute("SET statement_timeout='900s'")
            with RAW.open("w") as handle:
                cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH CSV HEADER", handle)
        _log(f"写出 {RAW}（{RAW.stat().st_size / 1e6:.0f} MB）")
    frame = pd.read_csv(
        RAW,
        dtype={"symbol": "string", "oi": "float64", "volume": "float64", "turnover": "float64"},
    )
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame


def align_twin_rounding(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """把只差 ≤1 元的孪生成交额对齐成同一个值，返回改动条数。

    只碰**同一张合约**（归一后同键）同一天的重复记录；差额超过容差的原样留着，
    好让 `product_daily_turnover` 的守卫照常炸。
    """
    spread = frame.groupby(["canonical", "trade_date"], sort=False)["turnover"].agg(
        ["min", "max", "nunique"]
    )
    disagreeing = spread.loc[spread["nunique"] > 1]
    within = disagreeing.loc[(disagreeing["max"] - disagreeing["min"]) <= TWIN_TOLERANCE_YUAN]
    _log(
        f"孪生成交额不一致 {len(disagreeing)} 组，其中 ≤{TWIN_TOLERANCE_YUAN} 元 {len(within)} 组"
    )
    if within.empty:
        return frame, 0
    keyed = frame.set_index(["canonical", "trade_date"])
    aligned = keyed.index.map(within["max"])
    touched = pd.notna(aligned) & (keyed["turnover"].to_numpy() != aligned)
    keyed.loc[touched, "turnover"] = pd.Series(aligned, index=keyed.index)[touched]
    return keyed.reset_index(), int(touched.sum())


def main() -> int:
    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024**3, 6 * 1024**3))
    daily = fetch()
    _log(f"日线 {len(daily):,} 行，{daily['trade_date'].min()}..{daily['trade_date'].max()}")

    daily["canonical"] = [
        canonical_contract(symbol, trade_date)
        for symbol, trade_date in zip(daily["symbol"], daily["trade_date"])
    ]
    daily = daily.loc[daily["canonical"].notna() & daily["turnover"].notna()].copy()
    _log(f"可归一的月度合约行 {len(daily):,}")

    daily, touched = align_twin_rounding(daily)
    _log(f"对齐孪生舍入差 {touched} 条")

    turnover_product = product_daily_turnover(daily)
    _log(f"品种-日 {len(turnover_product):,}，品种 {turnover_product['product'].nunique()}")

    months = []
    cursor = FIRST_MONTH
    while cursor <= LAST_MONTH:
        months.append(cursor)
        cursor = next_month(cursor)
    universe_a = {month: universe_for_month(turnover_product, month_start=month) for month in months}
    history = tuple(sorted({p for picked in universe_a.values() for p in picked}))
    _log(f"读法 A 历史品种 {len(history)}：{' '.join(history)}")

    choices = choose_dominant_commodity(
        daily.loc[:, ["trade_date", "symbol", "oi", "volume"]], products=history
    )
    _log(f"主力选择 {len(choices):,} 条")

    lookup = (
        daily.drop_duplicates(subset=["canonical", "trade_date"], keep="first")
        .set_index(["canonical", "trade_date"])["turnover"]
    )
    rows = []
    missing = 0
    for choice in choices:
        key = canonical_contract(choice.contract, choice.trade_date)
        try:
            value = lookup.at[(key, choice.trade_date)]
        except KeyError:
            missing += 1
            continue
        rows.append({"product": choice.product, "trade_date": choice.trade_date, "turnover": float(value)})
    _log(f"主力成交额 {len(rows):,} 条，查不到 {missing} 条")
    turnover_dominant = pd.DataFrame(rows)

    days_a = turnover_product["trade_date"].nunique()
    days_b = turnover_dominant["trade_date"].nunique()
    _log(f"交易日数 A={days_a} B={days_b}（D15 的分母，两者应当一致）")

    universe_b = {
        month: universe_for_month(turnover_dominant, month_start=month) for month in months
    }

    trace = pd.DataFrame(
        [
            {
                "month": month.isoformat(),
                "n_product_scope": len(universe_a[month]),
                "n_dominant_scope": len(universe_b[month]),
                "dropped": " ".join(sorted(set(universe_a[month]) - set(universe_b[month]))),
                "only_in_b": " ".join(sorted(set(universe_b[month]) - set(universe_a[month]))),
            }
            for month in months
        ]
    )
    OUT_TRACE.parent.mkdir(parents=True, exist_ok=True)
    trace.to_csv(OUT_TRACE, index=False)
    _log(f"写出 {OUT_TRACE}")

    share = turnover_dominant.merge(
        turnover_product, on=["product", "trade_date"], suffixes=("_dom", "_prod")
    )
    share["share"] = share["turnover_dom"] / share["turnover_prod"]
    share["year"] = [d.year for d in share["trade_date"]]
    summary = share.groupby("year")["share"].describe(percentiles=[0.1, 0.5, 0.9])
    summary.to_csv(OUT_SHARE)
    _log(f"写出 {OUT_SHARE}")
    print(summary.loc[:, ["count", "mean", "10%", "50%", "90%"]].to_string())

    print()
    print(trace.to_string(index=False, max_colwidth=60))
    print()
    print(
        "逐月品种数：A 均值 {:.1f}（{}..{}），B 均值 {:.1f}（{}..{}）".format(
            trace["n_product_scope"].mean(),
            trace["n_product_scope"].min(),
            trace["n_product_scope"].max(),
            trace["n_dominant_scope"].mean(),
            trace["n_dominant_scope"].min(),
            trace["n_dominant_scope"].max(),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
