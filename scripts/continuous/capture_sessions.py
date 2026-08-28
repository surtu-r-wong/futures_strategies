"""连续策略的时段规则采集驱动。

`config/carry_minute_sessions.csv` 的审计宇宙是 **Carry 的流动性池**；国信连续信号
用的是研报 §5.1「近半年日均成交额 ≥50 亿、按月重算」这个**另一个**宇宙。两者不重合，
于是全历史面板在 `2011-05-17 SHFE AL` 因为没有时段规则而中止。

本驱动复用 `scripts/carry/capture_minute_sessions.py` 的整条发布路径（授权核对、
边界采集、分类、原子写盘），只把「审计哪些品种日」换成连续策略自己的宇宙，并写到
**另一份资产**，`config/carry_minute_sessions.csv` 一个字节不动。

设计：`docs/superpowers/specs/2026-08-28-continuous-session-rules-backfill-design.md`
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cta_continuous.panel import required_session_keys_by_month  # noqa: E402
from cta_continuous.scope import DAILY_FROM, panel_scope  # noqa: E402
from common.minute.sessions import SessionClockError  # noqa: E402
from scripts.carry.capture_minute_sessions import (  # noqa: E402
    SessionCaptureError,
    build_audit,
)

#: 宇宙口径要回看半年；日线起点必须早于采集起点至少这么多。
UNIVERSE_LOOKBACK_MONTHS = 6

__all__ = [
    "audit_builder_for",
    "capture_keys",
    "continuous_capture_coverage",
    "main",
    "month_start",
]


def month_start(value: date) -> date:
    """采集按日期给区间，宇宙按月重算 —— 折到该日期所属自然月的 1 号。"""
    return date(value.year, value.month, 1)


def capture_keys(
    stats: pd.DataFrame, *, start: date, end: date
) -> frozenset[tuple[str, str, date]]:
    """采集必须覆盖的 `(exchange, product, trade_date)`。

    这就是 `build_contexts` 会向 `resolve_session_rule` 索取的那个集合 —— 不是另写
    一份等价逻辑，而是调同一个 `required_session_keys_by_month`（设计 D4）。
    """
    scope = panel_scope(stats, start=start, end=end)
    by_month = required_session_keys_by_month(
        choices=scope.choices, products_by_month=scope.products_by_month
    )
    return frozenset(key for keys in by_month.values() for key in keys)


def _require_universe_prewarm(start: date) -> None:
    """日线起点必须早于采集起点半年以上，否则首月宇宙是错的。

    Carry 的 `liquidity_history_incomplete` 闸在这里不适用：`universe_for_month`
    明文把品种缺席日按 0 计，历史不全只会**低估**成交额、绝不会误纳，那个失效模式
    在本口径下不存在。真正要保证的只有回看窗口够长这一条。
    """
    months = (start.year - DAILY_FROM.year) * 12 + (start.month - DAILY_FROM.month)
    if months < UNIVERSE_LOOKBACK_MONTHS:
        raise SessionCaptureError(
            "continuous_universe_prewarm: "
            f"daily_from={DAILY_FROM.isoformat()} start={start.isoformat()}; "
            f"the turnover universe looks back {UNIVERSE_LOOKBACK_MONTHS} months"
        )


def audit_builder_for(keys: frozenset[tuple[str, str, date]]):
    """把连续宇宙的键集包成一个 `audit_builder`，签名与 Carry 的那个一致。"""

    def build(prices, *, history_starts, history_exceptions, start, end, config):
        _require_universe_prewarm(start)

        def resolve(*, representative_index, normalized_keys, global_calendar):
            pool = frozenset(key for key in keys if start <= key[2] <= end)
            unknown = pool - set(normalized_keys)
            if unknown:
                # 面板要这些品种日，但采集侧的行情里没有它们。宁可炸，不要静默少采
                # —— 少采就是把同一个覆盖缺口原样留到下一次全历史跑。
                sample = sorted(unknown)[:5]
                raise SessionCaptureError(
                    "continuous_pool_outside_normalized: "
                    f"{len(unknown)} product-days the panel needs are absent from "
                    f"the capture's daily frame; e.g. {sample}"
                )
            first_by_identity: dict[tuple[str, str], date] = {}
            for exchange, product, trade_date in sorted(normalized_keys):
                first_by_identity.setdefault((exchange, product), trade_date)
            history = {
                (exchange, product, first): "lookback_complete"
                for (exchange, product), first in first_by_identity.items()
            }
            return pool, history

        return build_audit(
            prices, resolve_pool=resolve, start=start, end=end, config=config
        )

    return build


def continuous_capture_coverage(
    *, capture_start: date, backtest_start: date, prewarm_calendar_days: int
) -> date:
    """连续面板的覆盖不变量：资产首日不得晚于面板首月。

    Carry 的那条（资产必须早于回测首日 730 天）说的是**分钟状态机的预热**。连续面板
    没有这回事 —— 它从资产首日开始逐月造 bar，所以那条不变量它永远满足不了，也不该
    去满足。`prewarm_calendar_days` 只为签名统一而保留，本规则不使用它。
    """
    if capture_start > backtest_start:
        raise SessionClockError(
            exchange="*",
            product="*",
            trade_date=backtest_start,
            check="session_asset_starts_after_panel",
            reason=(
                "session asset begins after the panel's first month; "
                f"capture_start={capture_start.isoformat()}; "
                f"panel_start={backtest_start.isoformat()}"
            ),
        )
    return capture_start


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    from common.config import load_config, resolve_settings_path
    from common.db import pg_config_from
    from cta_continuous.scope import load_scope_daily
    from scripts.carry.capture_minute_sessions import capture_and_publish

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--panel-start", type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--inventory-output", required=True, type=Path)
    parser.add_argument("--audit-report", required=True, type=Path)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--use-test", action="store_true")
    args = parser.parse_args(argv)
    panel_start = args.panel_start or args.start

    settings_path = args.settings or resolve_settings_path()
    pg = pg_config_from(load_config(settings_path), use_test=args.use_test)
    stats = load_scope_daily(pg, end=args.end)
    print(f"日线 {len(stats):,} 行", flush=True)

    keys = capture_keys(
        stats, start=month_start(args.start), end=month_start(args.end)
    )
    products = sorted({key[1] for key in keys})
    print(
        f"连续宇宙 {len(keys):,} 个品种日，{len(products)} 个品种",
        flush=True,
    )

    counts = capture_and_publish(
        start=args.start,
        end=args.end,
        backtest_start=panel_start,
        output=args.output,
        inventory_output=args.inventory_output,
        audit_report=args.audit_report,
        settings=args.settings,
        use_test=args.use_test,
        audit_builder=audit_builder_for(keys),
        coverage_check=continuous_capture_coverage,
    )
    print(
        "products={} rules={} checked_days={} ambiguous={}".format(*counts),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
