"""Accept or reject one daily target sheet before it is traded.

Usage: .venv/bin/python scripts/check_daily_sheet.py output/targets/tsindex_<END>.xlsx

The criterion that matters is not "the last day's bmom_ready is True" -- that one
stays green through the defect it was meant to catch. `vol_scale` is 0.15 over the
annualised vol of the last 252 shadow returns, so the strict-history gate has to
have been open across that whole window: a window that mixes carry-only days with
blended ones reads the vol low and over-levers the book, silently. The 60-day
start that shipped on 2026-09-11 had 195 such days, read 4.13% against a converged
4.77% and moved 47 of 49 rows, one of them by 42 lots.

The last 252 panel dates stand in for the 252 shadow returns; they differ by at
most the one-day lag between a signal and the return it earns.

Exit code is 0 when the sheet is fit to trade, 1 when it is not.
"""

import sys

import pandas as pd

VOL_WINDOW = 252


def main(path: str) -> int:
    signals = pd.read_excel(path, sheet_name="signals")
    targets = pd.read_excel(path, sheet_name="next_targets")
    signal_date = signals["trade_date"].max()
    ok = True

    print(f"signal_date          {signal_date.date()}")
    print(f"品种数 / 行数         {targets['product'].nunique()} / {len(targets)}")
    print(
        f"vol_scale            {targets['vol_scale'].iloc[0]:.4f}"
        f"   毛敞口 {targets['target_weight'].abs().sum():.4f}"
    )

    if "bmom_ready" not in signals.columns:
        print("✗ 基差动量腿没开：signals 里没有 bmom_ready 列")
        ok = False
    else:
        dates = sorted(signals["trade_date"].unique())
        if len(dates) < VOL_WINDOW:
            print(f"✗ 面板只有 {len(dates)} 个交易日，不足 {VOL_WINDOW}：起跑窗太短")
            ok = False
        else:
            window = signals[signals["trade_date"].isin(dates[-VOL_WINDOW:])]
            share = window.groupby("trade_date")["bmom_ready"].mean()
            dead = int((share == 0).sum())
            print(f"{VOL_WINDOW} 日影子窗内 bmom_ready 全 False 的天数  {dead}   (必须是 0)")
            print(f"  窗口内每日通过率  最低 {share.min():.1%}  最高 {share.max():.1%}")
            if dead:
                print("✗ 窗口里混进了纯 carry 的日子 ⇒ 波动读低、账本过杠杆。起跑窗太短。")
                ok = False
        last = signals[signals["trade_date"] == signal_date]
        refused = sorted(last.loc[~last["bmom_ready"].astype(bool), "product"])
        print(f"  最后一天未过闸的品种（上市不足 500 日属正常）  {len(refused)}/{len(last)}  {refused}")

    rounded_out = targets[(targets["lots"] == 0) & (targets["target_weight"].abs() > 1e-9)]
    print(f"目标权重非零却取整成 0 手  {len(rounded_out)}  {list(rounded_out['product'])}   (应为 0)")
    ok = ok and rounded_out.empty

    print("\n" + ("✅ 通过，可以照这张单下" if ok else "❌ 不通过，别照这张单下"))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1]))
