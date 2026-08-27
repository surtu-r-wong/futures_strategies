"""Task 7 EXPLAIN-only 冒烟：真候选 → 共享分钟层的计划闸。不取任何数据行。"""
import json, sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2

sys.path.insert(0, "/home/elfbob/claude-code/futures_strategies")
from common.config import load_config, resolve_settings_path          # noqa: E402
from common.db import pg_config_from                                   # noqa: E402
from common.minute.pg_source import PublicMinuteSource                 # noqa: E402
from index_open_momentum.pg_source import (                            # noqa: E402
    build_index_candidates, choose_dominant, reconcile_dominant,
)
from index_open_momentum.sessions import load_index_session_rules      # noqa: E402

SH = ZoneInfo("Asia/Shanghai")
pg = pg_config_from(load_config(resolve_settings_path()), use_test=False)

def fetch(sql, params):
    conn = psycopg2.connect(host=pg["host"], port=pg["port"], dbname=pg["name"],
                            user=pg["user"], password=pg["password"])
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='120s'")
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=cols)
    finally:
        conn.close()

for label, lo, hi in [
    ("晚年代 2016-06", date(2016, 5, 25), date(2016, 7, 1)),
    ("早年代 2012-06", date(2012, 5, 25), date(2012, 7, 1)),
    ("尾部 2026-06", date(2026, 5, 25), date(2026, 7, 1)),
]:
    daily = fetch(
        """SELECT trade_date, symbol, oi, volume FROM public.futures_daily
           WHERE trade_date >= %s AND trade_date < %s
             AND (symbol LIKE 'IF%%' OR symbol LIKE 'IC%%' OR symbol LIKE 'IH%%')""",
        (lo, hi),
    )
    if daily.empty:
        print(f"\n=== {label} === 日线为空，跳过"); continue
    products = tuple(sorted({s.split(".")[0].rstrip("0123456789") for s in daily["symbol"]}))
    choices = choose_dominant(daily, products=products)
    month_start = date(lo.year, lo.month + 1, 1)
    month_end = date(month_start.year + (month_start.month == 12),
                     month_start.month % 12 + 1, 1)
    choices = tuple(c for c in choices if month_start <= c.trade_date < month_end)

    ref = fetch(
        """SELECT trade_date, base_symbol, contract_used FROM public.continuous_contract_ohlc
           WHERE rule_type='standard' AND base_symbol IN ('IF','IC','IH')
             AND trade_date >= %s AND trade_date < %s""",
        (month_start, month_end),
    )
    reference = {(r.trade_date, r.base_symbol): r.contract_used
                 for r in ref.itertuples() if r.contract_used}
    choices = reconcile_dominant(choices, reference=reference)
    candidates = build_index_candidates(choices, rules=load_index_session_rules())

    lower = datetime(month_start.year, month_start.month, 1, tzinfo=SH)
    upper = datetime(month_end.year, month_end.month, 1, tzinfo=SH)
    source = PublicMinuteSource(pg=pg)
    summary = source.explain_month(candidates, lower, upper)

    agree = [c.agrees for c in choices]
    print(f"\n=== {label} ===")
    print(f"  候选 contract-day: {len(candidates)}   品种: {products}")
    print(f"  与连续合约: 一致 {agree.count(True)} / 不一致 {agree.count(False)} / 无参照 {agree.count(None)}")
    print(f"  引用 chunk: {len(summary.referenced_chunks)} -> {summary.referenced_chunks}")
    print(f"  最大预计行数: {summary.maximum_plan_rows}")
    print(f"  节点类型: {summary.node_types}")
    bad = [c for c in choices if c.agrees is False]
    for c in bad[:5]:
        print(f"    ⚠️ 分歧 {c.trade_date} {c.product}: 自选 {c.contract} vs 参照 {c.reference_contract}")
