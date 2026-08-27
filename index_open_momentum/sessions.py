"""CFFEX 时段版本与两道数据质量闸 —— 计划 Task 1。

分钟层的通用机器在 `common/minute/sessions.py`，本模块只做三件本策略自己的事：

1. 把 `cffex-v1` 这个版本接到真资产 `config/index_minute_sessions.csv` 上；
2. **已知缺口登记**：ruleset 记的是交易所**官方**时段，而本地分钟档案在 2016 前
   短了 15:00–15:14 那 15 分钟。差额若不显式登记，任何"每个 slot 都该有 bar"的
   完整性校验会在 2016 前**每一个**交易日上硬失败；
3. **覆盖度闸**：逐年交易日数与交易所日历对账，缺口超阈值就不许标 paper-faithful。

⚠️ 登记表描述的是**本地档案**的缺陷，不是交易所事实。ruleset 一侧不许为了迁就档案
而把 915 改成 900 —— 那样 15:00 之后真出现一根 bar 时反而会被当成非法数据拒掉。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from common.minute.sessions import (
    CFFEX_V1,
    SessionClockError,
    SessionRule,
    SessionRuleset,
    load_session_rules,
)

__all__ = [
    "CFFEX_V1",
    "CFFEX_ARCHIVE_GAPS",
    "CoverageShortfall",
    "CoverageVerdict",
    "INDEX_SESSION_RULES_PATH",
    "KnownGap",
    "LISTING_DATES",
    "coverage_gate",
    "expected_absent_minutes",
    "known_gaps_on",
    "load_index_session_rules",
    "require_bars_map_to_slots",
]

INDEX_SESSION_RULES_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "index_minute_sessions.csv"
)

# 各品种挂牌日。覆盖度闸拿它当分母的起点 —— 用全年交易日当分母会把 IC/IH/IM
# 的上市当年判成巨额缺口。
LISTING_DATES: Mapping[str, date] = {
    "IF": date(2010, 4, 16),
    "IC": date(2015, 4, 16),
    "IH": date(2015, 4, 16),
    "IM": date(2022, 7, 22),
}


def load_index_session_rules(
    path: Path | None = None,
    *,
    ruleset: SessionRuleset = CFFEX_V1,
) -> tuple[SessionRule, ...]:
    """读版本化时段资产。默认是本仓的 `cffex-v1` 真资产。"""
    return load_session_rules(path or INDEX_SESSION_RULES_PATH, ruleset=ruleset)


# ---------------------------------------------------------------------------
# 已知缺口登记
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownGap:
    """本地分钟档案相对交易所官方时段的一段授权缺席。

    `reason` 不是注释，是交付物：保真度报告要逐条披露，所以登记项必须自带人话。
    """

    key: str
    reason: str
    effective_start: date
    effective_end: date | None
    start_minute: int
    end_minute: int

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.reason.strip():
            raise ValueError("known_gap_identity: key 与 reason 都必须非空")
        if type(self.effective_start) is not date or (
            self.effective_end is not None and type(self.effective_end) is not date
        ):
            raise ValueError("known_gap_dates: 生效日期必须是确切 date")
        if self.effective_end is not None and self.effective_start > self.effective_end:
            raise ValueError(
                "known_gap_date_order: effective_start 不得晚于 effective_end"
            )
        if self.end_minute <= self.start_minute:
            raise ValueError("known_gap_minutes: end_minute 必须晚于 start_minute")

    def covers(self, trade_date: date) -> bool:
        return self.effective_start <= trade_date and (
            self.effective_end is None or trade_date <= self.effective_end
        )

    @property
    def minutes(self) -> frozenset[int]:
        return frozenset(range(self.start_minute, self.end_minute))


CFFEX_ARCHIVE_GAPS: tuple[KnownGap, ...] = (
    KnownGap(
        key="cffex_pre2016_close_tail",
        reason=(
            "本库 2016 前每个交易日最后一根 bar 是 14:59，而中金所当时交易至 15:15："
            "15:00–15:14 这 15 分钟档案没有。不影响开盘信号，影响 2011~2015 的日内"
            "收盘平仓价口径，必须在保真度报告显式披露。"
        ),
        effective_start=date(2010, 4, 16),
        effective_end=date(2015, 12, 31),
        start_minute=900,
        end_minute=915,
    ),
)


def known_gaps_on(
    trade_date: date,
    *,
    gaps: Sequence[KnownGap] = CFFEX_ARCHIVE_GAPS,
) -> tuple[KnownGap, ...]:
    """当日生效的已知缺口。"""
    return tuple(gap for gap in gaps if gap.covers(trade_date))


def expected_absent_minutes(
    trade_date: date,
    *,
    gaps: Sequence[KnownGap] = CFFEX_ARCHIVE_GAPS,
) -> frozenset[int]:
    """当日预期缺席的分钟偏移集合。完整性校验应当先减掉它再判缺。"""
    absent: set[int] = set()
    for gap in known_gaps_on(trade_date, gaps=gaps):
        absent |= gap.minutes
    return frozenset(absent)


# ---------------------------------------------------------------------------
# 时间戳 → 时段映射
# ---------------------------------------------------------------------------


def require_bars_map_to_slots(
    *,
    bar_times: Iterable[datetime],
    slots: Sequence[datetime],
    rule: SessionRule,
) -> None:
    """每根 bar 的时间戳都必须落在唯一一个交易分钟槽上，否则硬失败。

    不做任何"数已有行数、把时间压到能对上"的补救 —— 对不上就是数据或时段规则
    有一个是错的，两种都必须当场炸出来。
    """
    trade_date = getattr(slots, "trade_date", rule.effective_start)
    allowed = set(slots)
    seen: set[datetime] = set()

    for bar_time in bar_times:
        if bar_time.tzinfo is None or bar_time.utcoffset() is None:
            raise SessionClockError(
                exchange=rule.exchange,
                product=rule.product,
                trade_date=trade_date,
                check="session_bar_naive",
                reason=f"bar timestamp must be timezone-aware; got {bar_time!r}",
            )
        if bar_time in seen:
            raise SessionClockError(
                exchange=rule.exchange,
                product=rule.product,
                trade_date=trade_date,
                check="session_bar_duplicate",
                reason=f"bar timestamp appears more than once: {bar_time.isoformat()}",
            )
        if bar_time not in allowed:
            raise SessionClockError(
                exchange=rule.exchange,
                product=rule.product,
                trade_date=trade_date,
                check="session_bar_unmapped",
                reason=(
                    "bar timestamp maps to no trading minute of "
                    f"{rule.version}: {bar_time.isoformat()}"
                ),
            )
        seen.add(bar_time)


# ---------------------------------------------------------------------------
# 覆盖度闸
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageShortfall:
    product: str
    year: int
    expected: int
    observed: int
    missing_dates: tuple[date, ...]

    @property
    def missing(self) -> int:
        return self.expected - self.observed


@dataclass(frozen=True)
class CoverageVerdict:
    paper_faithful: bool
    shortfalls: tuple[CoverageShortfall, ...]


def coverage_gate(
    *,
    calendar_dates: Sequence[date],
    observed_dates: Mapping[str, Sequence[date]],
    window_start: date,
    window_end: date,
    max_missing_days: int = 0,
    listing_dates: Mapping[str, date] = LISTING_DATES,
) -> CoverageVerdict:
    """逐年把观测到的交易日与交易所日历对账。

    默认 `max_missing_days=0`：fail-closed。放宽阈值只改 `paper_faithful` 一个
    判定，缺口本身照报不误 —— 闸门可以开，但账不许平掉。
    """
    if type(max_missing_days) is not int or max_missing_days < 0:
        raise ValueError("coverage_threshold: max_missing_days 必须是非负整数")
    if window_start > window_end:
        raise ValueError("coverage_window: window_start 不得晚于 window_end")

    calendar = {day for day in calendar_dates if window_start <= day <= window_end}

    shortfalls: list[CoverageShortfall] = []
    for product in sorted(observed_dates):
        try:
            listed = listing_dates[product]
        except KeyError as exc:
            raise ValueError(
                f"coverage_unknown_product: {product!r} 没有挂牌日；"
                f"已知 {tuple(sorted(listing_dates))!r}"
            ) from exc

        observed = set(observed_dates[product])
        stray = sorted(observed - calendar)
        if stray:
            raise ValueError(
                f"coverage_observed_not_in_calendar: {product} 的观测日 "
                f"{[day.isoformat() for day in stray]} 不在交易所日历的请求窗口内"
            )

        expected = {day for day in calendar if day >= listed}
        for year in sorted({day.year for day in expected}):
            expected_year = {day for day in expected if day.year == year}
            observed_year = {day for day in observed if day.year == year}
            missing = tuple(sorted(expected_year - observed_year))
            if missing:
                shortfalls.append(
                    CoverageShortfall(
                        product=product,
                        year=year,
                        expected=len(expected_year),
                        observed=len(observed_year),
                        missing_dates=missing,
                    )
                )

    worst = max((shortfall.missing for shortfall in shortfalls), default=0)
    return CoverageVerdict(
        paper_faithful=worst <= max_missing_days,
        shortfalls=tuple(shortfalls),
    )
