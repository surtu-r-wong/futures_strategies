"""H1 诊断：`Lev_ATR<1` 当成「持仓期逐 bar 离场」还是「开仓时点过滤」，差多少换手。

不改生产代码。三种读法用同一批指标、同一批闸旗跑三个状态机：
  wide        = D6 原样（逐 bar 独立判，四闸任一不过即空仓）
  narrow      = D21 现状（持仓期要 Lev_ATR>1 且均线方向不变）
  entryfilter = **未测过的读法**：Lev_ATR 只在开仓时点判，持仓期只看均线反向
                （依据：§3.1 自我定义为「决定开仓与否」，表 3 评的是开仓过滤信号，
                 §5.1 构建清单没有「平仓条件」这一项）

出处：2026-09-01 换手裁决复盘。产出写进
`docs/research/2026-08-31-continuous-turnover-verdict.md` §十一（H1 那一支）。
在 WSL2 上对全历史面板 `panel_84d482d` 跑出。

    python -m scripts.continuous.<本文件> <START> <END> [PANEL_DIR]
    START/END 是分片名尾部的 YYYY-MM，闭区间。
"""

from __future__ import annotations

import glob
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cta_continuous.backtest import BacktestParams  # noqa: E402
from common.leverage import atr_leverage  # noqa: E402
from cta_continuous.indicators import (  # noqa: E402
    atr_series, delta_tnr, ema, gap_widening, tnr_series,
)
from cta_continuous.signals import (  # noqa: E402
    ATR_LEVERAGE_FLOOR, U2P_THRESHOLD, Direction, gate_flags, up_down_prob,
)

START, END = sys.argv[1], sys.argv[2]
PANEL_DIR = sys.argv[3] if len(sys.argv) > 3 else "output/continuous/panel_84d482d"
P = BacktestParams()
WARM = P.warmup_bars

files = [f for f in sorted(glob.glob(f"{PANEL_DIR}/*.parquet"))
         if START <= f[-15:-8] <= END]
if not files:
    raise SystemExit(f"{PANEL_DIR} 在 {START}..{END} 内没有分片")

panel = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
print(f"{len(files)} 个分片，{len(panel):,} 行，{panel['product'].nunique()} 品种", flush=True)

def _value(direction, u2p):
    return (1.0 + u2p) / 2.0 if direction is Direction.LONG else -(1.0 - u2p) / 2.0

rows = []
exit_causes = {"lev_atr": 0, "ma_reversal": 0, "both": 0}
for (product, segment), frame in panel.groupby(["product", "continuity_segment"], sort=True):
    frame = frame.sort_values("slot_end")
    traded = frame.loc[~frame["no_trade"].astype(bool) & frame["close"].notna()]
    if len(traded) <= WARM:
        continue
    factor = traded["adj_factor"].to_numpy(float)
    closes = traded["close"].to_numpy(float) * factor
    highs = traded["high"].to_numpy(float) * factor
    lows = traded["low"].to_numpy(float) * factor
    short, long = ema(closes, span=P.ema_short), ema(closes, span=P.ema_long)
    wide_gap = gap_widening(short, long)
    dtnr = delta_tnr(tnr_series(closes, window=P.tnr_window), k=P.dtnr_k, mode=P.dtnr_mode)
    atr = atr_series(highs, lows, closes, window=P.atr_window)

    n = closes.size
    lev = np.array([atr_leverage(close=closes[i], atr=float(atr[i])) for i in range(n)])
    warm = np.array([i >= WARM and not math.isnan(dtnr[i]) for i in range(n)])
    bullish = short > long
    longs, shorts = [], []
    for i in range(n):
        if not warm[i]:
            longs.append(False); shorts.append(False); continue
        l, s = gate_flags(short_above_long=bool(bullish[i]), widening=bool(wide_gap[i]),
                          atr_leverage=float(lev[i]), delta_tnr=float(dtnr[i]))
        longs.append(l); shorts.append(s)
    u2p = np.array(up_down_prob(long_flags=longs, short_flags=shorts).u2p)

    entry_dir = []
    for i in range(n):
        if longs[i] and u2p[i] > U2P_THRESHOLD:
            entry_dir.append(Direction.LONG)
        elif shorts[i] and u2p[i] < -U2P_THRESHOLD:
            entry_dir.append(Direction.SHORT)
        else:
            entry_dir.append(Direction.FLAT)

    def run(hold_needs_lev: bool, count_causes: bool = False):
        vals, held = [], Direction.FLAT
        for i in range(n):
            if entry_dir[i] is not Direction.FLAT:
                held = entry_dir[i]; vals.append(_value(held, u2p[i])); continue
            if held is not Direction.FLAT:
                ma_ok = (held is Direction.LONG) == bool(bullish[i])
                lev_ok = float(lev[i]) > ATR_LEVERAGE_FLOOR
                alive = ma_ok and (lev_ok or not hold_needs_lev)
                if alive:
                    vals.append(_value(held, u2p[i])); continue
                if count_causes:
                    if not lev_ok and not ma_ok: exit_causes["both"] += 1
                    elif not lev_ok:             exit_causes["lev_atr"] += 1
                    else:                        exit_causes["ma_reversal"] += 1
                held = Direction.FLAT
            vals.append(0.0)
        return np.array(vals)

    wide_v = np.array([_value(entry_dir[i], u2p[i]) if entry_dir[i] is not Direction.FLAT
                       else 0.0 for i in range(n)])
    narrow_v = run(True, count_causes=True)
    entryf_v = run(False)

    days = traded["trade_date"].nunique()
    for name, v in (("wide", wide_v), ("narrow", narrow_v), ("entryfilter", entryf_v)):
        d = np.abs(np.diff(v, prepend=0.0))
        rows.append({
            "reading": name, "product": product, "segment": segment,
            "bars": n, "days": days,
            "sig_turnover": d.sum(),
            "changes": int((d > 1e-12).sum()),
            "in_pos_bars": int((np.abs(v) > 0).sum()),
        })

r = pd.DataFrame(rows)
g = r.groupby("reading").agg(bars=("bars", "sum"), days=("days", "sum"),
                             sig_turnover=("sig_turnover", "sum"),
                             changes=("changes", "sum"), in_pos=("in_pos_bars", "sum"))
g["每品种日信号换手"] = g.sig_turnover / g.days
g["每品种日变动次数"] = g.changes / g.days
g["持仓 bar 占比"] = g.in_pos / g.bars
g["相对 narrow"] = g.sig_turnover / g.loc["narrow", "sig_turnover"]
pd.set_option("display.width", 200)
print()
print(f"=== {START} .. {END} ===")
print(g[["每品种日信号换手", "每品种日变动次数", "持仓 bar 占比", "相对 narrow"]].to_string(
    float_format=lambda v: f"{v:,.4f}"))
tot = sum(exit_causes.values())
print()
print(f"narrow 的离场成因（共 {tot:,} 次）：")
for k, v in exit_causes.items():
    print(f"  {k:<12} {v:>9,}  {v/tot:6.1%}")
