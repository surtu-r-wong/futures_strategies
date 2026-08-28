"""全历史 15 分钟面板构建（计划 Task 3 Step 6）。

按月分批、逐月落盘。中途失败可以续跑：已存在的月份分片直接跳过
（[[long-jobs-need-setsid]] 的教训 —— 长跑必须能 dump 中间结果）。

用法：
    python scripts/continuous/build_panel.py --start 2011-01 --end 2026-01 \
        --out output/continuous/panel
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.config import load_config, resolve_settings_path  # noqa: E402
from common.db import pg_config_from  # noqa: E402
from common.minute.pg_source import PublicMinuteSource  # noqa: E402
from common.minute.sessions import load_session_rules  # noqa: E402
from cta_carry.session_authority import load_pricing_bases, pricing_basis_for  # noqa: E402
from cta_continuous.continuous import adjustment_factors  # noqa: E402
from cta_continuous.panel import (  # noqa: E402
    build_contexts,
    build_panel,
    context_choices_for_month,
    require_session_coverage,
)
from cta_continuous.scope import load_scope_daily, panel_scope  # noqa: E402

SESSION_RULES = Path("config/continuous_minute_sessions.csv")
PRICING_BASES = Path("config/carry_minute_pricing_basis.csv")


def _month(value: str) -> date:
    year, month = value.split("-")
    return date(int(year), int(month), 1)


def _next_month(anchor: date) -> date:
    return date(anchor.year + anchor.month // 12, anchor.month % 12 + 1, 1)


def _validate_existing_shard(path: Path) -> None:
    expected_column = "continuity_segment"
    expected_dtype = "int64"
    try:
        segment = pd.read_parquet(path, columns=[expected_column])
        observed_dtype = str(segment[expected_column].dtype)
    except Exception as exc:
        raise ValueError(
            "panel_shard_schema_mismatch: "
            f"path={path} expected_column={expected_column} "
            f"expected_dtype={expected_dtype}"
        ) from exc
    if observed_dtype != expected_dtype:
        raise ValueError(
            "panel_shard_schema_mismatch: "
            f"path={path} expected_column={expected_column} "
            f"expected_dtype={expected_dtype} observed={observed_dtype}"
        )


def _existing_shard_can_be_skipped(path: Path) -> bool:
    if not path.exists():
        return False
    _validate_existing_shard(path)
    return True


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

    stats = load_scope_daily(pg, end=args.end)
    print(f"日线 {len(stats):,} 行", flush=True)
    # 口径与补采驱动共用，见 cta_continuous/scope.py 的模块注释（设计 D4）。
    scope = panel_scope(stats, start=args.start, end=args.end)
    products_by_month = scope.products_by_month
    choices = scope.choices
    closes = {
        (trade_date, str(symbol)): float(close)
        for trade_date, symbol, close in stats.loc[
            stats["close"].notna(), ["trade_date", "symbol", "close"]
        ].itertuples(index=False, name=None)
    }
    factor_rows = adjustment_factors(choices, closes=closes)
    segment_by_key = {
        (row.trade_date, row.product): int(row.continuity_segment)
        for row in factor_rows.itertuples(index=False)
    }
    segment_total = factor_rows[["product", "continuity_segment"]].drop_duplicates().shape[0]
    break_total = segment_total - factor_rows["product"].nunique()
    factor_by_key = {
        (row.trade_date, row.product): float(row.adj_factor)
        for row in factor_rows.itertuples(index=False)
    }
    print(
        f"全历史主力 {len(choices):,} 个品种日，后复权因子 {len(factor_by_key):,} 个，"
        f"连续段 {segment_total:,}，断代 {break_total:,}",
        flush=True,
    )

    # ⚠️ 闸必须挡在**任何**分钟查询之前。覆盖不全要在这里一次报全，不能跑到第 N 个月
    # 才崩：2026-08-27 那轮全历史面板先崩在 `2010-12-31 DCE A`，补掉后又崩在
    # `2011-05-17 SHFE AL`，两次都是同一个覆盖问题换个地方露头，中间白跑了数小时。
    require_session_coverage(
        choices=choices,
        products_by_month=products_by_month,
        rules=rules,
        manifest_path=args.out / "session_coverage_gaps.csv",
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
        if _existing_shard_can_be_skipped(shard):
            print(f"{month:%Y-%m} 已存在，跳过", flush=True)
            month = _next_month(month)
            continue

        started = time.monotonic()
        products = products_by_month[month]
        if not products:
            print(f"{month:%Y-%m} 宇宙为空，跳过", flush=True)
            month = _next_month(month)
            continue

        eligible_choices = tuple(
            choice for choice in choices if choice.product in products
        )
        this_month = [
            choice
            for choice in eligible_choices
            if choice.trade_date.year == month.year
            and choice.trade_date.month == month.month
        ]
        if not this_month:
            print(f"{month:%Y-%m} 当月无主力，跳过", flush=True)
            month = _next_month(month)
            continue

        context_choices = context_choices_for_month(
            eligible_choices, month_start=month
        )
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
            adjustment_factor_by_key={
                key: factor_by_key[key] for key in contexts
            },
            continuity_segment_by_key={key: segment_by_key[key] for key in contexts},
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
