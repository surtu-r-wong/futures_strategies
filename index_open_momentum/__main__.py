"""国信开盘动量 CLI —— 计划 Task 7 step 4。

本模块只做**参数面**：解析、校验、以及把"哪些组合必须当场拒绝"钉死。编排真跑
的部分调用 `pg_source` / `bars` / `backtest`，报告由 Task 8 接手。

## 三道当场拒绝的闸

1. **区间越过分钟表** —— `futures_minute` 最新 bar 是 2026-08-11，越过就是在要不存在的数据；
2. **区间跨越日线表的边界** —— `futures_daily` 连续覆盖只到 2026-04-29，其后主力要改从
   分钟表推。跨界要两条通路各跑一段再拼；**静默拼接会让换月规则在中间悄悄变一次**，
   所以这里拒绝，逼调用方分两次跑并显式说明拼接口；
3. **区间落在 2016 前却硬要 paper-faithful** —— 那段缺 15:00–15:15
   （`sessions.CFFEX_ARCHIVE_GAPS`），结构上标不了。

⚠️ 这三个上界都是**实测出来的日期**，不是配置。它们会随上游补数而移动，移动时
连同 `docs/research/2026-08-27-cffex-session-crosscheck.md` 一起改。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date

from common.minute.sessions import SESSION_RULESETS
from index_open_momentum.pg_source import INDEX_PRODUCTS
from index_open_momentum.sessions import CFFEX_ARCHIVE_GAPS, LISTING_DATES

#: 研报的品种池。IM 2022-07-22 才上市，在研报之后 —— 默认不进。
PAPER_PRODUCTS = ("IF", "IC", "IH")

#: `futures_minute` 最新 bar（2026-08-27 实测）。
MINUTE_TABLE_LAST = date(2026, 8, 11)

#: `futures_daily` 连续覆盖的末日（2026-08-27 **逐月**实测；
#: 它的 `max(trade_date)` 是 2026-08-26，但那是孤零零一天，别当覆盖）。
DAILY_TABLE_LAST = date(2026, 4, 29)

DEFAULT_SESSION_VERSION = "cffex-v1"


@dataclass(frozen=True)
class Options:
    start: date
    end: date
    products: tuple[str, ...]
    session_version: str
    output_prefix: str
    require_paper_faithful: bool
    paper_faithful_possible: bool
    dominant_source: str
    on_unpriceable: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m index_open_momentum",
        description="国信开盘动量（股指期货日内）复刻",
    )
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--products",
        default=",".join(PAPER_PRODUCTS),
        help=f"逗号分隔，默认研报口径 {','.join(PAPER_PRODUCTS)}",
    )
    parser.add_argument("--session-version", default=DEFAULT_SESSION_VERSION)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument(
        "--on-unpriceable",
        choices=("abort", "skip"),
        default="abort",
        help=(
            "必需的 5 分钟成交窗零成交时：abort=按计划口径硬失败（默认）；"
            "skip=当日不建仓并计数（不顺延窗口、不替换价格）"
        ),
    )
    parser.add_argument(
        "--require-paper-faithful",
        action="store_true",
        help="区间结构上标不了 paper-faithful 时直接拒绝运行",
    )
    return parser


def _fail(message: str) -> None:
    raise SystemExit(f"index_open_momentum: {message}")


def _earliest_listing(products: tuple[str, ...]) -> date:
    return min(LISTING_DATES[product] for product in products)


def _window_has_archive_gap(start: date, end: date) -> bool:
    return any(
        gap.effective_start <= end
        and (gap.effective_end is None or start <= gap.effective_end)
        for gap in CFFEX_ARCHIVE_GAPS
    )


def resolve_options(namespace: argparse.Namespace) -> Options:
    start: date = namespace.start
    end: date = namespace.end
    if end < start:
        _fail(f"--end {end} 早于 --start {start}")

    products = tuple(
        part.strip().upper() for part in namespace.products.split(",") if part.strip()
    )
    if not products:
        _fail("--products 不能为空")
    unknown = [p for p in products if p not in INDEX_PRODUCTS]
    if unknown:
        _fail(f"--products 里有本仓不支持的品种 {unknown}；支持 {list(INDEX_PRODUCTS)}")

    if namespace.session_version not in SESSION_RULESETS:
        _fail(
            f"--session-version {namespace.session_version!r} 未注册；"
            f"已注册 {sorted(SESSION_RULESETS)}"
        )
    if namespace.session_version != DEFAULT_SESSION_VERSION:
        _fail(
            f"--session-version {namespace.session_version!r} 不是股指口径；"
            f"本策略只认 {DEFAULT_SESSION_VERSION!r}"
        )

    listing = _earliest_listing(products)
    if start < listing:
        _fail(f"--start {start} 早于所选品种的最早挂牌日 {listing}")
    if end > MINUTE_TABLE_LAST:
        _fail(f"--end {end} 越过分钟表末端 {MINUTE_TABLE_LAST}")

    if end <= DAILY_TABLE_LAST:
        dominant_source = "daily"
    elif start > DAILY_TABLE_LAST:
        dominant_source = "minute"
    else:
        _fail(
            f"区间跨越日线表边界 {DAILY_TABLE_LAST}：主力合约在边界两侧来自不同的表。"
            "请分两段跑并在报告里显式说明拼接口 —— 静默拼接会让换月规则在中间变一次"
        )

    # 研报样本是 IF/IC/IH；带上 IM 就不再是研报口径。
    universe_is_paper = set(products) <= set(PAPER_PRODUCTS)
    paper_faithful_possible = universe_is_paper and not _window_has_archive_gap(
        start, end
    )
    if namespace.require_paper_faithful and not paper_faithful_possible:
        reason = (
            "品种池含研报之外的合约"
            if not universe_is_paper
            else f"区间覆盖已登记的档案缺口 {CFFEX_ARCHIVE_GAPS[0].key}"
        )
        _fail(f"--require-paper-faithful 与该区间不相容：{reason}")

    return Options(
        start=start,
        end=end,
        products=products,
        session_version=namespace.session_version,
        output_prefix=namespace.output_prefix,
        require_paper_faithful=bool(namespace.require_paper_faithful),
        paper_faithful_possible=paper_faithful_possible,
        dominant_source=dominant_source,
        on_unpriceable=namespace.on_unpriceable,
    )


def _fetch(sql: str, params: tuple, pg: dict):
    import pandas as pd
    import psycopg2

    connection = psycopg2.connect(
        host=pg["host"],
        port=pg["port"],
        dbname=pg["name"],
        user=pg["user"],
        password=pg["password"],
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='300s'")
            cursor.execute(sql, params)
            columns = [column[0] for column in cursor.description]
            return pd.DataFrame(cursor.fetchall(), columns=columns)
    finally:
        connection.close()


#: 主力选取的 warmup 自然日数。滞后一个**交易日**，但长假可能连着十天，
#: 所以按自然日多取一截；多取的部分选完就被滤掉。
SELECTION_WARMUP_DAYS = 20


def _dominant_inputs(options: Options, pg: dict):
    """按 `dominant_source` 取主力合约的原料。见模块 docstring 的取数边界。

    ⚠️ 起点往前多取 `SELECTION_WARMUP_DAYS` 天：主力选取滞后一个交易日，不多取的话
    **`--start` 当天就没有前一交易日可用**，会被静默丢掉 —— 覆盖度闸当场会报"缺 1 天"，
    而那其实不是数据缺陷。
    """
    from datetime import timedelta

    products = tuple(options.products)
    if options.dominant_source == "daily":
        like = " OR ".join(f"symbol LIKE '{p}%%'" for p in products)
        return _fetch(
            f"""SELECT trade_date, symbol, oi, volume FROM public.futures_daily
                WHERE trade_date >= %s AND trade_date <= %s AND ({like})""",
            (options.start - timedelta(days=SELECTION_WARMUP_DAYS), options.end),
            pg,
        )
    raise SystemExit(
        "index_open_momentum: 尾部（2026-04-29 之后）的主力合约通路"
        "（名单取 futures_contract_info、量取 futures_minute）尚未接线"
    )


def main(argv: list[str] | None = None) -> int:
    options = resolve_options(build_parser().parse_args(argv))
    print(
        f"区间 {options.start}~{options.end} / 品种 {','.join(options.products)} / "
        f"时段 {options.session_version} / 主力来源 {options.dominant_source} / "
        f"paper-faithful {'可' if options.paper_faithful_possible else '不可'}"
    )

    from datetime import date as _date

    from common.config import load_config, resolve_settings_path
    from common.db import pg_config_from
    from common.minute.pg_source import PublicMinuteSource
    from index_open_momentum.pg_source import (
        build_index_candidates,
        choose_dominant,
        metadata_multipliers_for,
        reconcile_dominant,
    )
    from index_open_momentum.report import fidelity_verdict, write_outputs
    from index_open_momentum.run import run_backtest
    from index_open_momentum.sessions import load_index_session_rules

    pg = pg_config_from(load_config(resolve_settings_path()), use_test=False)

    daily = _dominant_inputs(options, pg)
    choices = choose_dominant(daily, products=options.products)
    choices = tuple(c for c in choices if options.start <= c.trade_date <= options.end)

    reference_rows = _fetch(
        """SELECT trade_date, base_symbol, contract_used
           FROM public.continuous_contract_ohlc
           WHERE rule_type='standard' AND trade_date >= %s AND trade_date <= %s""",
        (options.start, min(options.end, _date(2026, 4, 29))),
        pg,
    )
    reference = {
        (row.trade_date, row.base_symbol): row.contract_used
        for row in reference_rows.itertuples()
        if row.contract_used
    }
    choices = reconcile_dominant(choices, reference=reference)
    disagreements = [c for c in choices if c.agrees is False]
    print(
        f"主力候选 {len(choices)}；与连续合约不一致 {len(disagreements)}（只报告，不改选）"
    )

    rules = load_index_session_rules()
    candidates = build_index_candidates(choices, rules=rules)
    result = run_backtest(
        candidates=candidates,
        rules=rules,
        source=PublicMinuteSource(pg=pg),
        metadata_multipliers=metadata_multipliers_for(candidates),
        on_unpriceable=options.on_unpriceable,
    )
    if result.unpriceable_sessions:
        print(
            f"⚠️ 无法计价的交易日 {result.unpriceable_sessions} 个"
            f"（占 {100 * result.unpriceable_sessions / max(1, len(result.product_days)):.2f}%），"
            "已当日不建仓并计入报告"
        )
    # ⚠️ 覆盖度闸在这里才真正生效：逐年交易日与交易所日历对账。
    # `trading_calendar` **没有 CFFEX 列**，用 `sfe` —— 2010–2025 逐年与 IF/IC/IH/IM
    # 日线行数一个不差（见研究档 §二）。
    calendar_rows = _fetch(
        """SELECT calendar_date FROM public.trading_calendar
           WHERE sfe AND deleted_at IS NULL
             AND calendar_date >= %s AND calendar_date <= %s""",
        (options.start, options.end),
        pg,
    )
    fidelity = fidelity_verdict(
        result,
        calendar_dates=tuple(calendar_rows["calendar_date"]),
        window_start=options.start,
        window_end=options.end,
        window_allows_paper_faithful=options.paper_faithful_possible,
    )
    print(f"paper-faithful 判定：{fidelity.paper_faithful}")
    for reason in fidelity.reasons:
        print(f"  否决：{reason}")
    if options.require_paper_faithful and not fidelity.paper_faithful:
        raise SystemExit(
            "index_open_momentum: --require-paper-faithful 但闸未通过（理由见上）"
        )

    paths = write_outputs(
        result, output_prefix=options.output_prefix, fidelity=fidelity
    )
    for path in paths:
        print(f"  写出 {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
