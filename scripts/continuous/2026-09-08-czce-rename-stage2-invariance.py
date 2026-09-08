"""郑商所代码归一 Stage 2（全部四位）——本地重放裁判。

Stage 1（交割月 ≤1701）的裁判见 `2026-09-02-czce-rename-invariance.py` 与
`-postcheck.py`。Stage 2 于 2026-09-03 已执行，所以这里只能做**事后**复验。

⚠️ 不能拿 09-02 那份快照直接当"改前"：09-02..09-08 之间窗口内还回填了 4,632 行
（大商所 08-27..09-01 等），两份的差里混着**数据变化**，分不清哪一部分是改名。

所以改前那一份**从今天这批行自己重建**：Stage 2 只动 ≥1702 的拼写，而 `exch_symbol`
存着交易所原始码 ⇒ 改前拼写 = 交割月 ≥1702 时取 `exch_symbol`，否则原样。
两份是**同一批行的两种拼写**，任何差异只能来自改名。

同时把 09-02 那份也跑一遍，回答另一个问题：那 4,632 行新数据自己动了什么。
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

BEFORE_STAGE2 = Path("output/continuous/futures_daily_scope_20260902_post.csv")
AFTER_STAGE2 = Path("output/continuous/futures_daily_scope_20260908.csv")
FIRST_MONTH = date(2011, 1, 1)
LAST_MONTH = date(2026, 1, 1)

_HEAD = re.compile(r"^([A-Za-z]+)(\d+)$")


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def pre_stage2_spelling(symbol: str, exch_symbol: object) -> str:
    """Stage 2 之前这一行的 symbol 长什么样。"""
    if not symbol.endswith(".CZC") or type(exch_symbol) is not str:
        return symbol
    match = _HEAD.fullmatch(symbol.split(".", 1)[0])
    if match is None or len(match.group(2)) != 4:
        return symbol
    if int(match.group(2)) < 1702:  # Stage 1 已经归四位，Stage 2 没动
        return symbol
    return f"{exch_symbol}.CZC"


def load(path: Path, *, spelling: str) -> pd.DataFrame:
    usecols = ["symbol", "trade_date", "oi", "volume", "turnover"]
    if spelling == "pre_stage2":
        usecols.append("exch_symbol")
    frame = pd.read_csv(
        path,
        usecols=usecols,
        dtype={
            "symbol": "string",
            "exch_symbol": "string",
            "oi": "float64",
            "volume": "float64",
            "turnover": "float64",
        },
    )
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    if spelling == "pre_stage2":
        frame["symbol"] = [
            pre_stage2_spelling(symbol, exch)
            for symbol, exch in zip(frame["symbol"], frame["exch_symbol"])
        ]
        frame = frame.drop(columns=["exch_symbol"])
    raw_rows = len(frame)
    frame["canonical"] = [
        canonical_contract(symbol, trade_date)
        for symbol, trade_date in zip(frame["symbol"], frame["trade_date"])
    ]
    unparsed = int(frame["canonical"].isna().sum())
    frame = frame.loc[frame["canonical"].notna() & frame["turnover"].notna()].copy()
    _log(
        f"{path.name}[{spelling}]：{raw_rows:,} 行读入，归一失败 {unparsed:,}，留下 {len(frame):,}"
    )
    return frame


def replay(frame: pd.DataFrame, label: str):
    turnover = product_daily_turnover(frame)
    months = []
    cursor = FIRST_MONTH
    while cursor <= LAST_MONTH:
        months.append(cursor)
        cursor = next_month(cursor)
    universe = {
        month: universe_for_month(turnover, month_start=month) for month in months
    }
    history = tuple(sorted({p for picked in universe.values() for p in picked}))
    _log(f"{label}：品种-日 {len(turnover):,}，历史宇宙 {len(history)} 个品种")
    choices = choose_dominant_commodity(
        frame.loc[:, ["trade_date", "symbol", "oi", "volume"]], products=history
    )
    _log(f"{label}：主力选择 {len(choices):,} 条")
    keyed = {
        (c.trade_date, c.product): canonical_contract(c.contract, c.trade_date)
        for c in choices
    }
    spelling = {(c.trade_date, c.product): c.contract for c in choices}
    return turnover, universe, history, keyed, spelling


def compare(left, right, *, left_label: str, right_label: str) -> None:
    t_l, u_l, h_l, k_l, s_l = left
    t_r, u_r, h_r, k_r, s_r = right
    print("=" * 78)
    print(f"### {left_label}  vs  {right_label}")
    print(f"历史宇宙品种集合相同：{set(h_l) == set(h_r)}（{len(h_l)} / {len(h_r)}）")
    diff_months = [m for m in u_l if u_l[m] != u_r[m]]
    print(f"{len(u_l)} 个月里宇宙成员不同的月份：{len(diff_months)}")
    for month in diff_months[:10]:
        print(
            f"  {month}: +{sorted(set(u_r[month]) - set(u_l[month]))} -{sorted(set(u_l[month]) - set(u_r[month]))}"
        )
    print(f"主力选择：{len(k_l):,} / {len(k_r):,}，键集合相同：{set(k_l) == set(k_r)}")
    only_l = sorted(set(k_l) - set(k_r))[:5]
    only_r = sorted(set(k_r) - set(k_l))[:5]
    if only_l or only_r:
        print(f"  只在左边的键（前 5）：{only_l}")
        print(f"  只在右边的键（前 5）：{only_r}")
    disagree = [key for key in k_l if key in k_r and k_l[key] != k_r[key]]
    print(f"同一 (日, 品种) 选出不同**归一合约**：{len(disagree)} 条")
    for key in disagree[:10]:
        print(f"  {key}: {k_l[key]} -> {k_r[key]}")
    respelled = [key for key in s_l if key in s_r and s_l[key] != s_r[key]]
    print(f"（拼写层）主力字符串不同：{len(respelled):,} 条")
    for key in respelled[:5]:
        print(f"  {key}: {s_l[key]} -> {s_r[key]}")
    merged = t_l.merge(
        t_r,
        on=["product", "trade_date"],
        how="outer",
        suffixes=("_l", "_r"),
        indicator=True,
    )
    print(f"品种-日 键差异：{int((merged['_merge'] != 'both').sum()):,} 条")
    both = merged.loc[merged["_merge"] == "both"]
    delta = (both["turnover_l"] - both["turnover_r"]).abs()
    print(
        f"品种-日成交额：不等 {int((delta > 0).sum()):,} 条，最大绝对差 {delta.max():.2f} 元"
    )
    print()


def main() -> int:
    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024**3, 6 * 1024**3))

    before = load(AFTER_STAGE2, spelling="pre_stage2")

    # 重建规则的独立验证：与 09-02 真快照在共有 (归一合约, 交易日) 上逐条对拼写
    snapshot = load(BEFORE_STAGE2, spelling="as_is")
    key = ["canonical", "trade_date"]
    joined = (
        before.set_index(key)["symbol"]
        .to_frame("rebuilt")
        .join(snapshot.set_index(key)["symbol"].to_frame("actual"), how="inner")
    )
    mismatch = joined.loc[joined["rebuilt"] != joined["actual"]]
    _log(
        f"重建校验：与 09-02 快照共有 {len(joined):,} 行，拼写不同 {len(mismatch):,} 行"
    )
    for row in mismatch.head(5).itertuples():
        print(f"  {row.Index}: 重建 {row.rebuilt} vs 实际 {row.actual}")

    del joined, mismatch
    print()
    r_before = replay(before, "改前拼写（重建）")
    del before
    r_snapshot = replay(snapshot, "09-02 快照")
    del snapshot
    after = load(AFTER_STAGE2, spelling="as_is")
    r_after = replay(after, "改后拼写（真实）")
    del after
    print()

    compare(
        r_before, r_after, left_label="改前拼写", right_label="改后拼写（同一批行）"
    )
    compare(
        r_snapshot, r_after, left_label="09-02 快照", right_label="09-08 真实（含回填）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
