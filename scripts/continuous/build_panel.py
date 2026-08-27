"""全历史 15 分钟面板构建（计划 Task 3 Step 6）。

按月分批、逐月落盘。中途失败可以续跑：已存在的月份分片直接跳过
（[[long-jobs-need-setsid]] 的教训 —— 长跑必须能 dump 中间结果）。

用法：
    python scripts/continuous/build_panel.py --start 2011-01 --end 2026-01 \
        --out output/continuous/panel
"""

from __future__ import annotations

import argparse
import io
import sys
import time
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
from cta_continuous.panel import (  # noqa: E402
    build_contexts,
    build_panel,
    context_choices_for_month,
)
from cta_continuous.universe import (  # noqa: E402
    product_daily_turnover,
    universe_for_month,
)

SESSION_RULES = Path("config/carry_minute_sessions.csv")
PRICING_BASES = Path("config/carry_minute_pricing_basis.csv")


def _month(value: str) -> date:
    year, month = value.split("-")
    return date(int(year), int(month), 1)


def _next_month(anchor: date) -> date:
    return date(anchor.year + anchor.month // 12, anchor.month % 12 + 1, 1)


def _copy(cur, sql: str, columns: list[str]) -> pd.DataFrame:
    buffer = io.StringIO()
    cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH CSV HEADER", buffer)
    frame = pd.read_csv(io.StringIO(buffer.getvalue()))
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame.loc[:, columns]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, type=_month)
    parser.add_argument("--end", required=True, type=_month)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    cfg = load_config(resolve_settings_path())
    pg = pg_config_from(cfg)
    rules = load_session_rules(SESSION_RULES)
    bases = load_pricing_bases(PRICING_BASES)

    # 宇宙要看过去半年，主力要看展期链，所以日线一次拉够，往前多取一年。
    daily_from = date(args.start.year - 1, args.start.month, 1)
    with get_connection(pg) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout='900s'")
        stats = _copy(
            cur,
            "SELECT symbol, trade_date, oi, volume, turnover FROM public.futures_daily "
            f"WHERE trade_date >= DATE '{daily_from}' AND trade_date < DATE '{_next_month(args.end)}' "
            "AND oi IS NOT NULL AND volume IS NOT NULL",
            ["symbol", "trade_date", "oi", "volume", "turnover"],
        )
    print(f"日线 {len(stats):,} 行", flush=True)
    turnover = product_daily_turnover(
        stats.loc[:, ["symbol", "trade_date", "turnover"]]
    )

    source = PublicMinuteSource(pg=pg)

    def resolve_multiplier(candidate, frame):
        return source.resolve_metadata_multiplier(
            daily_contract=candidate.daily_contract,
            trade_date=candidate.trade_date,
            frame=frame,
            inference_frame=frame,
            pricing_basis=pricing_basis_for(bases, candidate.exchange),
        ).multiplier

    month = args.start
    while month <= args.end:
        shard = args.out / f"panel-{month:%Y-%m}.parquet"
        if shard.exists():
            print(f"{month:%Y-%m} 已存在，跳过", flush=True)
            month = _next_month(month)
            continue

        started = time.monotonic()
        products = universe_for_month(turnover, month_start=month)
        if not products:
            print(f"{month:%Y-%m} 宇宙为空，跳过", flush=True)
            month = _next_month(month)
            continue

        # 主力链需要跨月连续，所以给它前后各留一个月的日线。
        window = stats.loc[
            (stats["trade_date"] >= date(month.year - 1, month.month, 1))
            & (stats["trade_date"] < _next_month(month))
        ]
        keep = (
            window["symbol"].str.upper().str.replace(r"[^A-Z].*$", "", regex=True)
        ).isin(products)
        try:
            choices = choose_dominant_commodity(window.loc[keep], products=products)
        except ValueError as error:
            print(f"{month:%Y-%m} 主力选择失败：{error}", flush=True)
            month = _next_month(month)
            continue

        this_month = [c for c in choices if c.trade_date.month == month.month
                      and c.trade_date.year == month.year]
        if not this_month:
            print(f"{month:%Y-%m} 当月无主力，跳过", flush=True)
            month = _next_month(month)
            continue

        context_choices = context_choices_for_month(choices, month_start=month)
        contexts = build_contexts(context_choices, rules=rules)
        contexts = {
            key: value for key, value in contexts.items()
            if key[0].year == month.year and key[0].month == month.month
        }
        panel = build_panel(
            contexts=contexts,
            source=source,
            pricing_basis_by_exchange={
                exchange: pricing_basis_for(bases, exchange)
                for exchange in {c.candidate.exchange for c in contexts.values()}
            },
            multiplier_resolver=resolve_multiplier,
        )
        panel.to_parquet(shard, index=False)
        print(
            f"{month:%Y-%m} 品种 {len(products)} 上下文 {len(contexts)} "
            f"bar {len(panel):,} 用时 {time.monotonic() - started:.1f}s",
            flush=True,
        )
        month = _next_month(month)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
