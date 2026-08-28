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

import csv
import hashlib
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.minute.bars import MinuteDataError  # noqa: E402
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
    "BlockedCandidate",
    "continuous_capture_coverage",
    "main",
    "month_start",
    "read_boundary_cache",
    "survey_boundaries",
    "write_blocked_manifest",
    "write_boundary_cache",
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


@dataclass(frozen=True)
class BlockedCandidate:
    """一个采集不到分钟观测的品种日。"""

    exchange: str
    product: str
    trade_date: date
    daily_contract: str
    check: str
    reason: str


def survey_boundaries(candidates, *, capture, on_month=None):
    """逐月采集边界观测；月内有品种日缺分钟数据时二分定位，记账后继续采其余。

    权威采集撞上第一个缺数据的候选就抛，而它**按月批量**查，于是一个坏候选会让整月
    中止、每轮只能知道一个。普查要一次拿到完整清单再统一裁决，所以失败的批做二分。

    ⚠️ **二分必须限制在月内。** `capture_session_boundaries` 内部本来就按月分组，
    若在全量候选上二分，一个坏候选会把整份列表对半重查 —— 每层重读约 n 行、深度约
    log2(n)，单个失败就是十几倍的基础成本。按月切开之后，重查代价被限制在那一个月，
    完好的月份一次查完、零额外开销。

    ⚠️ **不使用 `tolerate_empty`。** 它的文档串明写 "must never be set for the
    audited representative" —— 打开后缺数据的行会被当成「已授权的缺席」保留下来送进
    分类器，那是在把缺陷伪装成授权，比不做还危险。

    `on_month(month, captured, blocked)` 是心跳钩子：3 小时的长跑没有它就判不了活体。

    返回 `(拿到的观测, 缺数据的品种日)`。
    """
    frames = []
    blocked: list[BlockedCandidate] = []

    def walk(batch):
        if not batch:
            return
        try:
            frames.append(capture(batch))
            return
        except (MinuteDataError, SessionCaptureError) as exc:
            if len(batch) == 1:
                item = batch[0].candidate
                blocked.append(
                    BlockedCandidate(
                        exchange=item.exchange,
                        product=item.product,
                        trade_date=item.trade_date,
                        daily_contract=item.daily_contract,
                        check=getattr(exc, "check", "unknown"),
                        reason=str(exc),
                    )
                )
                return
        middle = len(batch) // 2
        walk(batch[:middle])
        walk(batch[middle:])

    by_month: dict[tuple[int, int], list] = {}
    for item in candidates:
        trade_date = item.candidate.trade_date
        by_month.setdefault((trade_date.year, trade_date.month), []).append(item)

    for month in sorted(by_month):
        before = len(blocked)
        walk(by_month[month])
        if on_month is not None:
            on_month(month, len(by_month[month]), len(blocked) - before)

    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return frame, tuple(blocked)


def write_blocked_manifest(blocked, path: Path) -> None:
    """把清单落盘供逐条裁决 —— 这份文件是普查唯一的产物，它不接发布路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["exchange", "product", "trade_date", "daily_contract", "check", "reason"]
        )
        for item in sorted(
            blocked, key=lambda row: (row.trade_date, row.exchange, row.product)
        ):
            writer.writerow(
                [
                    item.exchange,
                    item.product,
                    item.trade_date.isoformat(),
                    item.daily_contract,
                    item.check,
                    item.reason.replace("\n", " "),
                ]
            )


def _digest_path(path: Path) -> Path:
    return path.with_name(path.name + ".digest")


def boundary_cache_digest(keys) -> str:
    """键集的指纹 —— 缓存属于哪一次采集，靠它认。"""
    payload = "\n".join(
        f"{exchange}/{product}/{trade_date.isoformat()}"
        for exchange, product, trade_date in sorted(keys)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: 边界帧里以 python ``date`` 存在的列。parquet 推不出 ``object`` 里装的 ``date``，
#: 必须显式转成 datetime64 再写、读回后转回来 —— 否则整条缓存路径在真实数据上才炸。
_DATE_COLUMNS = ("trade_date", "previous_trade_date")


def write_boundary_cache(frame, path: Path, *, keys) -> None:
    """把观测与键集指纹一起落盘，供权威采集复用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = frame.copy()
    for column in _DATE_COLUMNS:
        if column in payload.columns:
            # 显式钉住 ns：date 转出来是秒级，fastparquet 写时会因不可无损
            # 转换而拒绝（"Cannot losslessly cast ... to s"）。
            payload[column] = pd.to_datetime(payload[column]).astype("datetime64[ns]")
    payload.to_parquet(path, index=False)
    _digest_path(path).write_text(boundary_cache_digest(keys), encoding="utf-8")


def read_boundary_cache(path: Path, *, keys):
    """读缓存；不存在返回 ``None``，键集对不上则**拒绝**。

    观测最贵的一步就是查分钟表，所以普查的观测值得复用。但复用的前提是「这份观测确实
    属于这次采集」—— 键集变了却照用，等于拿旧宇宙的观测发布新资产。所以对不上就抛，
    不静默重查、也不静默沿用（沿用 `b19103b` 拒绝陈旧分片的姿态）。
    """
    if not path.exists():
        return None
    digest_path = _digest_path(path)
    expected = boundary_cache_digest(keys)
    actual = (
        digest_path.read_text(encoding="utf-8").strip()
        if digest_path.exists()
        else None
    )
    if actual != expected:
        raise SessionCaptureError(
            f"boundary_cache_stale: path={path} expected={expected} observed={actual}"
        )
    restored = pd.read_parquet(path)
    for column in _DATE_COLUMNS:
        if column in restored.columns:
            restored[column] = pd.to_datetime(restored[column]).dt.date
    return restored


def _capture_context(*, settings, use_test):
    """采集要用的授权表、日线与分钟源 —— 普查与权威采集共用同一套入参。"""
    from cta_carry.config import CarryConfig
    from cta_carry.pg_source import (
        load_public_carry_data,
        load_public_product_history_starts,
    )
    from common.minute.pg_source import PublicMinuteSource
    from scripts.carry.capture_minute_sessions import (
        ABSENT_PRODUCT_DAYS_PATH,
        DAY_ONLY_PATH,
        HISTORY_EXCEPTIONS_PATH,
        SESSION_EXCEPTIONS_PATH,
    )
    from cta_carry.session_authority import load_session_authority

    authority = load_session_authority(
        session_exception_path=SESSION_EXCEPTIONS_PATH,
        day_only_path=DAY_ONLY_PATH,
        history_exception_path=HISTORY_EXCEPTIONS_PATH,
        absent_product_day_path=ABSENT_PRODUCT_DAYS_PATH,
    )
    return (
        CarryConfig(),
        authority,
        load_public_carry_data,
        load_public_product_history_starts,
        PublicMinuteSource,
    )


def run_survey(*, keys, start, end, settings, use_test, manifest, cache):
    """只读普查：一次跑出完整的「缺分钟观测」清单，**不接发布路径**。

    产物只有两样：阻塞清单，以及（清单为空时）可供权威采集复用的边界观测缓存。

    ⚠️ 缓存是整份的：清单非空时不写。也就是说一轮裁决之后重跑普查仍要重新观测一遍；
    真正省下的是「普查 → 权威采集」这一跳，权威采集不必再查一遍分钟表。
    """
    from scripts.carry.capture_minute_sessions import capture_session_boundaries

    config, authority, load_daily, load_history, minute_source = _capture_context(
        settings=settings, use_test=use_test
    )
    data = load_daily(
        start=start, end=end, config=config, config_path=settings, use_test=use_test
    )
    history_starts = load_history(config_path=settings, use_test=use_test)
    audit = audit_builder_for(keys)(
        data.prices,
        history_starts=history_starts,
        history_exceptions=authority.liquidity_history_exceptions,
        start=start,
        end=end,
        config=config,
    )
    source = minute_source(config_path=settings, use_test=use_test)
    absent = frozenset(
        (row.trade_date, row.exchange, row.product)
        for row in authority.absent_product_days
    )
    print(f"普查候选 {len(audit.candidates):,} 个", flush=True)

    def capture(batch):
        return capture_session_boundaries(source, batch, absent_identities=absent)

    started = time.monotonic()

    def heartbeat(month, size, newly_blocked):
        # 判活体靠这行；没有它，3 小时的长跑与卡死无法区分。
        print(
            f"{month[0]}-{month[1]:02d} 候选 {size} 阻塞 {newly_blocked} "
            f"累计 {time.monotonic() - started:.0f}s",
            flush=True,
        )

    frame, blocked = survey_boundaries(
        audit.candidates, capture=capture, on_month=heartbeat
    )
    write_blocked_manifest(blocked, manifest)
    if cache is not None and not blocked:
        write_boundary_cache(
            frame, cache, keys=frozenset(k for k in keys if start <= k[2] <= end)
        )
    return blocked


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
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inventory-output", type=Path)
    parser.add_argument("--audit-report", type=Path)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--use-test", action="store_true")
    parser.add_argument(
        "--survey",
        type=Path,
        metavar="MANIFEST",
        help="只读普查：把采集不到分钟观测的品种日一次列全到 MANIFEST，不发布任何资产",
    )
    parser.add_argument("--boundary-cache", type=Path)
    args = parser.parse_args(argv)
    panel_start = args.panel_start or args.start

    settings_path = args.settings or resolve_settings_path()
    pg = pg_config_from(load_config(settings_path), use_test=args.use_test)
    stats = load_scope_daily(pg, end=args.end)
    print(f"日线 {len(stats):,} 行", flush=True)

    keys = capture_keys(stats, start=month_start(args.start), end=month_start(args.end))
    print(
        f"连续宇宙 {len(keys):,} 个品种日，{len({key[1] for key in keys})} 个品种",
        flush=True,
    )

    if args.survey is not None:
        blocked = run_survey(
            keys=keys,
            start=args.start,
            end=args.end,
            settings=args.settings,
            use_test=args.use_test,
            manifest=args.survey,
            cache=args.boundary_cache,
        )
        print(f"survey blocked={len(blocked)} manifest={args.survey}", flush=True)
        return 1 if blocked else 0

    missing = [
        name
        for name, value in (
            ("--output", args.output),
            ("--inventory-output", args.inventory_output),
            ("--audit-report", args.audit_report),
        )
        if value is None
    ]
    if missing:
        parser.error(f"publishing requires {', '.join(missing)}")

    boundaries = None
    if args.boundary_cache is not None:
        boundaries = read_boundary_cache(
            args.boundary_cache,
            keys=frozenset(k for k in keys if args.start <= k[2] <= args.end),
        )
        hit = "hit" if boundaries is not None else "miss"
        print(f"boundary_cache={hit}", flush=True)

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
        boundaries=boundaries,
    )
    print(
        "products={} rules={} checked_days={} ambiguous={}".format(*counts),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
