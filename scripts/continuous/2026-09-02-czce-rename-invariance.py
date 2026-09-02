"""market-monitor 的郑商所改名（Stage 1）会不会动到连续信号的派生量 —— 本地重放裁判。

对方计划：`public.futures_daily` 里郑商所**交割月 ≤1701** 的合约代码一律四位
（有四位孪生的：并值后删三位那行；无孪生的：三位改名为四位），≥1702 一个字符不动。
对方断言「合约选择本身不应改变，变的只是键的拼写」。

**不采信复述，自己重放。** 用 09:38 拉下来的改前快照
`output/continuous/futures_daily_scope_20260902.csv`（2010-01-04..2026-01-30），
在本地按对方规则造出「改后」的那份，两份各跑一次

    日线 → 品种成交额 → 逐月宇宙 → 主力链

再逐条比。判据是**下游派生量**，不是行数（[[futures-daily-exchange-load-degraded]]）。
"""

from __future__ import annotations

import re
import resource
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cta_continuous.continuous import choose_dominant_commodity  # noqa: E402
from cta_continuous.scope import next_month  # noqa: E402
from cta_continuous.universe import (  # noqa: E402
    canonical_contract,
    product_daily_turnover,
    universe_for_month,
)

RAW = Path("output/continuous/futures_daily_scope_20260902.csv")
FIRST_MONTH = date(2011, 1, 1)
LAST_MONTH = date(2026, 1, 1)

THREE_DIGIT = re.compile(r"^([A-Z]{1,2})([0-9]{3})\.CZC$")
#: 对方的分界：交割月 ≤1701 归四位，≥1702 保持三位。
LAST_FOUR_DIGIT_DELIVERY = 1701


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load() -> pd.DataFrame:
    frame = pd.read_csv(
        RAW,
        dtype={"symbol": "string", "oi": "float64", "volume": "float64", "turnover": "float64"},
    )
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    frame["canonical"] = [
        canonical_contract(symbol, trade_date)
        for symbol, trade_date in zip(frame["symbol"], frame["trade_date"])
    ]
    return frame.loc[frame["canonical"].notna() & frame["turnover"].notna()].copy()


def align_twin_rounding(frame: pd.DataFrame) -> pd.DataFrame:
    """改前那份要先把 ≤1 元的孪生舍入差抹平，否则生产守卫会拦住重放。"""
    spread = frame.groupby(["canonical", "trade_date"], sort=False)["turnover"].agg(["min", "max", "nunique"])
    within = spread.loc[(spread["nunique"] > 1) & ((spread["max"] - spread["min"]) <= 1.0)]
    keyed = frame.set_index(["canonical", "trade_date"])
    mapped = pd.Series(keyed.index.map(within["max"]), index=keyed.index)
    touched = mapped.notna() & (keyed["turnover"] != mapped)
    keyed.loc[touched, "turnover"] = mapped[touched]
    _log(f"改前：对齐孪生舍入差 {int(touched.sum())} 条")
    return keyed.reset_index()


def apply_stage1(frame: pd.DataFrame) -> pd.DataFrame:
    """按对方规则造「改后」那份。"""
    delivery = []
    is_three = []
    for symbol, canonical in zip(frame["symbol"], frame["canonical"]):
        match = THREE_DIGIT.match(str(symbol))
        is_three.append(match is not None)
        delivery.append(int(canonical.split(":")[2]))
    work = frame.assign(is_three=is_three, delivery=delivery)
    target = work["is_three"] & (work["delivery"] <= LAST_FOUR_DIGIT_DELIVERY)
    _log(f"改后：命中三位且交割月 ≤{LAST_FOUR_DIGIT_DELIVERY} 的行 {int(target.sum()):,}")

    four_keys = set(
        zip(work.loc[~work["is_three"], "canonical"], work.loc[~work["is_three"], "trade_date"])
    )
    has_twin = [
        (canonical, trade_date) in four_keys
        for canonical, trade_date in zip(work["canonical"], work["trade_date"])
    ]
    work = work.assign(has_twin=has_twin)

    bucket_a = target & work["has_twin"]
    bucket_b = target & ~work["has_twin"]
    _log(f"改后：A 桶（删三位行）{int(bucket_a.sum()):,}，B 桶（改名）{int(bucket_b.sum()):,}")

    # A 桶：交易所值非空则覆盖四位那行的值，然后删掉三位行。
    donors = work.loc[bucket_a].set_index(["canonical", "trade_date"])
    survivors = work.loc[~bucket_a].copy()
    keyed = survivors.set_index(["canonical", "trade_date"])
    for column in ("turnover", "oi", "volume"):
        replacement = pd.Series(keyed.index.map(donors[column]), index=keyed.index)
        keyed.loc[replacement.notna(), column] = replacement[replacement.notna()]
    survivors = keyed.reset_index()

    # B 桶：三位改名为四位（canonical 不变，只换字符串）。
    mask = survivors["is_three"] & (survivors["delivery"] <= LAST_FOUR_DIGIT_DELIVERY)
    survivors.loc[mask, "symbol"] = [
        f"{canonical.split(':')[1]}{canonical.split(':')[2]}.CZC"
        for canonical in survivors.loc[mask, "canonical"]
    ]
    return survivors.drop(columns=["is_three", "delivery", "has_twin"])


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
    keyed = {
        (c.trade_date, c.product): canonical_contract(c.contract, c.trade_date) for c in choices
    }
    return turnover, universe, history, keyed


def main() -> int:
    resource.setrlimit(resource.RLIMIT_AS, (7 * 1024**3, 7 * 1024**3))
    raw = load()
    _log(f"快照 {len(raw):,} 行，{raw['trade_date'].min()}..{raw['trade_date'].max()}")

    before = align_twin_rounding(raw)
    after = apply_stage1(raw)
    _log(f"改后行数 {len(after):,}（比改前少 {len(before) - len(after):,}）")

    t_b, u_b, h_b, k_b = replay(before, "改前")
    t_a, u_a, h_a, k_a = replay(after, "改后")

    print()
    print("=" * 72)
    print(f"历史宇宙品种集合相同：{set(h_b) == set(h_a)}（改前 {len(h_b)} / 改后 {len(h_a)}）")
    diff_months = [m for m in u_b if u_b[m] != u_a[m]]
    print(f"181 个月里宇宙成员不同的月份：{len(diff_months)}")
    for month in diff_months[:10]:
        print(f"  {month}: 改前少了 {set(u_a[month]) - set(u_b[month])}，改后少了 {set(u_b[month]) - set(u_a[month])}")

    print(f"主力选择键数：改前 {len(k_b):,} / 改后 {len(k_a):,}，键集合相同：{set(k_b) == set(k_a)}")
    disagree = [key for key in k_b if key in k_a and k_b[key] != k_a[key]]
    print(f"同一 (日, 品种) 选出不同**归一合约**的条数：{len(disagree)}")
    for key in disagree[:10]:
        print(f"  {key}: {k_b[key]} -> {k_a[key]}")

    merged = t_b.merge(t_a, on=["product", "trade_date"], how="outer", suffixes=("_b", "_a"), indicator=True)
    only = merged.loc[merged["_merge"] != "both"]
    print(f"品种-日 键差异：{len(only)} 条")
    both = merged.loc[merged["_merge"] == "both"]
    delta = (both["turnover_b"] - both["turnover_a"]).abs()
    print(f"品种-日成交额：不等 {int((delta > 0).sum())} 条，最大绝对差 {delta.max():.2f} 元，"
          f"最大相对差 {(delta / both['turnover_a'].abs()).max():.3e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
