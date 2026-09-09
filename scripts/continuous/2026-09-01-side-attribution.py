"""多空归因：把毛收益拆成多头腿与空头腿，验证是否某一侧系统性亏钱（方向错误的指纹）。

损益口径与账户一致：按**合约**、用**原始成交价**，contribution = weight × (P_t/P_{t-1} − 1)。

出处：2026-09-01 口径审计（用户问「可能有正负号搞反」）。产出写进
`docs/research/2026-08-31-continuous-turnover-verdict.md` §11.3 —— 两腿都为正、
完全对称，但 2013/2014/2021 三年两腿精确对冲，指向横截面选择而非方向。
在 WSL2 上对全历史面板 `panel_84d482d`（2,906,872 行）跑出，日志 `logs/side_attrib.log`。
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cta_continuous.backtest import BacktestParams, run_backtest  # noqa: E402

PANEL_DIR = sys.argv[1] if len(sys.argv) > 1 else "output/continuous/panel_84d482d"

P = BacktestParams(ema_short=20, ema_long=120, tnr_window=20,
                   exit_gates="narrow", rebalance="monthly", cost_bps=0.0)
files = sorted(glob.glob(f"{PANEL_DIR}/*.parquet"))
if not files:
    raise SystemExit(f"面板目录没有分片：{PANEL_DIR}")
panel = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
print(f"面板 {len(panel):,} 行", flush=True)
res = run_backtest(panel, params=P)

ex = res.executions.copy()
print(f"executions {len(ex):,} 行，列 {list(ex.columns)}", flush=True)

# 用 executions 重建每张合约的持仓段：一次执行后持有 new_weight 直到下一次执行。
ex = ex.sort_values(["contract", "timestamp"])
g = ex.groupby("contract", sort=False)
ex["next_price"] = g["price"].shift(-1)
ex["next_ts"] = g["timestamp"].shift(-1)
seg = ex[ex.next_price.notna() & (ex.new_weight != 0.0)].copy()
seg["ret"] = seg.next_price / seg.price - 1.0
seg["pnl"] = seg.new_weight * seg.ret
seg["side"] = np.where(seg.new_weight > 0, "多头", "空头")
seg["year"] = pd.to_datetime(seg.timestamp).dt.year

tot = seg.groupby("side").agg(段数=("pnl","size"), 毛损益=("pnl","sum"),
                              平均权重=("new_weight", lambda s: s.abs().mean()))
tot["占毛损益"] = tot.毛损益 / seg.pnl.sum()
print()
print("=== 多空归因（零成本，20/120 + monthly + narrow，全历史）===")
print(tot.to_string(float_format=lambda v: f"{v:,.4f}"))
print(f"\n合计毛损益（对数尺度近似）= {seg.pnl.sum():.4f}")
print()
by = seg.pivot_table(index="year", columns="side", values="pnl", aggfunc="sum").fillna(0.0)
by["合计"] = by.sum(axis=1)
print("逐年：")
print(by.to_string(float_format=lambda v: f"{v:+.4f}"))
