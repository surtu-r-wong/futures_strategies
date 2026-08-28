"""小窗口真跑一次面板（计划 Task 3 Step 5）。

目的不是产出数据，是**接线**：EXPLAIN 是否触发 chunk exclusion、乘数能不能解出来、
郑商所是否走上 ohlc_typical、挂起的成交价是否跨日补上。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.config import load_config, resolve_settings_path  # noqa: E402
from common.db import get_connection, pg_config_from  # noqa: E402
from common.minute.pg_source import PublicMinuteSource  # noqa: E402
from common.minute.sessions import load_session_rules  # noqa: E402
from cta_carry.session_authority import load_pricing_bases, pricing_basis_for  # noqa: E402
from cta_continuous.continuous import choose_dominant_commodity  # noqa: E402
from cta_continuous.panel import build_contexts, build_panel  # noqa: E402

PRODUCTS = ("RB", "CU", "TA")          # 三个交易所各一个，郑商所在内
START, END = date(2023, 1, 1), date(2023, 3, 31)


def main() -> int:
    cfg = load_config(resolve_settings_path())
    rules = load_session_rules(Path("config/continuous_minute_sessions.csv"))
    bases = load_pricing_bases(Path("config/carry_minute_pricing_basis.csv"))

    with get_connection(pg_config_from(cfg)) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout='300s'")
        cur.execute(
            "SELECT symbol, trade_date, oi, volume FROM public.futures_daily "
            "WHERE trade_date >= %s AND trade_date <= %s AND oi IS NOT NULL "
            "AND volume IS NOT NULL",
            (START, END),
        )
        daily = pd.DataFrame(cur.fetchall(), columns=["symbol", "trade_date", "oi", "volume"])
    daily["oi"] = daily["oi"].astype("int64")
    daily["volume"] = daily["volume"].astype("int64")
    print(f"日线 {len(daily):,} 行")

    keep = daily["symbol"].str.upper().str.replace(r"[^A-Z].*$", "", regex=True).isin(PRODUCTS)
    choices = choose_dominant_commodity(daily.loc[keep], products=PRODUCTS)
    print(f"主力选择 {len(choices)} 条，展期 "
          f"{len({(c.product, c.contract) for c in choices})} 张合约")

    contexts = build_contexts(choices, rules=rules)
    print(f"品种-日上下文 {len(contexts)} 个")

    source = PublicMinuteSource(pg=pg_config_from(cfg))

    def resolve_multiplier(candidate, frame):
        basis = pricing_basis_for(bases, candidate.exchange)
        resolution = source.resolve_metadata_multiplier(
            daily_contract=candidate.daily_contract,
            trade_date=candidate.trade_date,
            frame=frame,
            inference_frame=frame,
            pricing_basis=basis,
        )
        return resolution.multiplier

    panel = build_panel(
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange={
            exchange: pricing_basis_for(bases, exchange)
            for exchange in {c.candidate.exchange for c in contexts.values()}
        },
        multiplier_resolver=resolve_multiplier,
    )
    out = Path("output/continuous/panel_smoke.parquet")
    panel.to_parquet(out, index=False)
    print(f"\n写出 {out}：{len(panel):,} 行")
    print(panel.groupby("product").agg(
        bars=("slot_end", "size"),
        no_trade=("no_trade", "sum"),
        pending=("fill_pending", "sum"),
        unpriceable=("fill_unpriceable", "sum"),
        basis=("pricing_basis", "first"),
        multiplier=("multiplier", "first"),
    ).to_string())

    print("\n查询计划：")
    for summary in source.plan_audit[:3]:
        print(f"  {summary}")
    print(f"  共 {len(source.plan_audit)} 条；source audit = {source.audit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
