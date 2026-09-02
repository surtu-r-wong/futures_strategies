"""改名执行完之后的复验：拿**真实的改后数据**，验此前基于替身的「逐点不变」预测。

上午那份 `2026-09-02-czce-rename-invariance.py` 是在本地按对方规则重写快照做的**预测**。
Stage 1 于 13:4x 实际执行后，这里换成

    改前 = 09:38 那份快照（`futures_daily_scope_20260902.csv`，落盘未动）
    改后 = 现在从库里重新拉的同一窗口

两份各跑一次 日线 → 品种成交额 → 逐月宇宙 → 主力链，逐条比派生量。

**改后那份不做任何孪生对齐** —— 若 `product_daily_turnover` 的严格相等守卫仍然炸，
说明重复没清干净，是复验的一部分而不是障碍。
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

RAW_BEFORE = Path("output/continuous/futures_daily_scope_20260902.csv")
RAW_AFTER = Path("output/continuous/futures_daily_scope_20260902_post.csv")
FIRST_MONTH = date(2011, 1, 1)
LAST_MONTH = date(2026, 1, 1)
DAILY_UNTIL = date(2026, 2, 1)


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def fetch_after() -> None:
    if RAW_AFTER.exists():
        _log(f"复用 {RAW_AFTER}")
        return
    cfg = load_config(resolve_settings_path())
    sql = (
        "SELECT symbol, trade_date, oi, volume, turnover "
        "FROM public.futures_daily "
        f"WHERE trade_date >= DATE '{DAILY_FROM}' AND trade_date < DATE '{DAILY_UNTIL}' "
        "AND oi IS NOT NULL AND volume IS NOT NULL"
    )
    with get_connection(pg_config_from(cfg)) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout='900s'")
        with RAW_AFTER.open("w") as handle:
            cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH CSV HEADER", handle)
    _log(f"写出 {RAW_AFTER}（{RAW_AFTER.stat().st_size / 1e6:.0f} MB）")


def load(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        path,
        dtype={"symbol": "string", "oi": "float64", "volume": "float64", "turnover": "float64"},
    )
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    frame["canonical"] = [
        canonical_contract(symbol, trade_date)
        for symbol, trade_date in zip(frame["symbol"], frame["trade_date"])
    ]
    return frame.loc[frame["canonical"].notna() & frame["turnover"].notna()].copy()


def twin_disagreements(frame: pd.DataFrame) -> int:
    spread = frame.groupby(["canonical", "trade_date"], sort=False)["turnover"].nunique()
    return int((spread > 1).sum())


def align_twin_rounding(frame: pd.DataFrame) -> pd.DataFrame:
    spread = frame.groupby(["canonical", "trade_date"], sort=False)["turnover"].agg(["min", "max", "nunique"])
    within = spread.loc[(spread["nunique"] > 1) & ((spread["max"] - spread["min"]) <= 1.0)]
    keyed = frame.set_index(["canonical", "trade_date"])
    mapped = pd.Series(keyed.index.map(within["max"]), index=keyed.index)
    touched = mapped.notna() & (keyed["turnover"] != mapped)
    keyed.loc[touched, "turnover"] = mapped[touched]
    _log(f"改前：对齐孪生舍入差 {int(touched.sum())} 条")
    return keyed.reset_index()


def replay(frame: pd.DataFrame, label: str):
    turnover = product_daily_turnover(frame)
    months = []
    cursor = FIRST_MONTH
    while cursor <= LAST_MONTH:
        months.append(cursor)
        cursor = next_month(cursor)
    universe = {month: universe_for_month(turnover, month_start=month) for month in months}
    history = tuple(sorted({p for picked in universe.values() for p in picked}))
    _log(f"{label}：品种-日 {len(turnover):,}，历史宇宙 {len(history)} 个品种")
    choices = choose_dominant_commodity(
        frame.loc[:, ["trade_date", "symbol", "oi", "volume"]], products=history
    )
    _log(f"{label}：主力选择 {len(choices):,} 条")
    keyed = {(c.trade_date, c.product): canonical_contract(c.contract, c.trade_date) for c in choices}
    spelling = {(c.trade_date, c.product): c.contract for c in choices}
    return turnover, universe, history, keyed, spelling


def main() -> int:
    resource.setrlimit(resource.RLIMIT_AS, (7 * 1024**3, 7 * 1024**3))
    fetch_after()

    raw_before = load(RAW_BEFORE)
    raw_after = load(RAW_AFTER)
    _log(f"改前 {len(raw_before):,} 行 / 改后 {len(raw_after):,} 行（差 {len(raw_before) - len(raw_after):,}）")

    before_twins = twin_disagreements(raw_before)
    after_twins = twin_disagreements(raw_after)
    _log(f"孪生成交额分歧：改前 {before_twins} 组，改后 **{after_twins}** 组")

    before = align_twin_rounding(raw_before)
    t_b, u_b, h_b, k_b, s_b = replay(before, "改前")
    t_a, u_a, h_a, k_a, s_a = replay(raw_after, "改后（真实）")

    print()
    print("=" * 72)
    print(f"守卫：改后未做任何对齐即通过 product_daily_turnover = {after_twins == 0}")
    print(f"历史宇宙品种集合相同：{set(h_b) == set(h_a)}（{len(h_b)} / {len(h_a)}）")
    diff_months = [m for m in u_b if u_b[m] != u_a[m]]
    print(f"181 个月里宇宙成员不同的月份：{len(diff_months)}")
    for month in diff_months[:10]:
        print(f"  {month}: +{sorted(set(u_a[month]) - set(u_b[month]))} -{sorted(set(u_b[month]) - set(u_a[month]))}")

    print(f"主力选择：改前 {len(k_b):,} / 改后 {len(k_a):,}，键集合相同：{set(k_b) == set(k_a)}")
    disagree = [key for key in k_b if key in k_a and k_b[key] != k_a[key]]
    print(f"同一 (日, 品种) 选出不同**归一合约**：{len(disagree)} 条")
    for key in disagree[:10]:
        print(f"  {key}: {k_b[key]} -> {k_a[key]}")

    respelled = [key for key in s_b if key in s_a and s_b[key] != s_a[key]]
    print(f"（预期会变的）主力**字符串拼写**改变：{len(respelled)} 条")
    for key in respelled[:5]:
        print(f"  {key}: {s_b[key]} -> {s_a[key]}")

    merged = t_b.merge(t_a, on=["product", "trade_date"], how="outer", suffixes=("_b", "_a"), indicator=True)
    print(f"品种-日 键差异：{int((merged['_merge'] != 'both').sum())} 条")
    both = merged.loc[merged["_merge"] == "both"]
    delta = (both["turnover_b"] - both["turnover_a"]).abs()
    print(f"品种-日成交额：不等 {int((delta > 0).sum())} 条，最大绝对差 {delta.max():.2f} 元")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
