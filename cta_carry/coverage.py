"""Where a daily run may end: the per-exchange coverage of public.futures_daily.

The table's overall max(trade_date) lies whenever one exchange lands late.
DCE has no scriptable daily feed and is delivered by hand, so most mornings
it is a day behind the other four; a run ending on the overall max would see
every DCE product without a bar and read the hole as a signal exit.  The
daily run therefore ends on the earliest of the commodity exchanges' max
dates, recomputed every time (never a stored date, never the overall max).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
import sys


COMMODITY_EXCHANGES = ("CZC", "DCE", "GFE", "INE", "SHF")


@dataclass(frozen=True)
class CoverageCutoff:
    cutoff: date
    latest: date
    lagging: dict[str, date]


def coverage_cutoff(max_dates: Mapping[str, date]) -> CoverageCutoff:
    """Earliest commodity-exchange max date, and which exchanges sit behind it."""
    absent = [exchange for exchange in COMMODITY_EXCHANGES if exchange not in max_dates]
    if absent:
        raise ValueError(
            f"no futures_daily rows for commodity exchange(s) {', '.join(absent)}"
        )
    dates = {exchange: max_dates[exchange] for exchange in COMMODITY_EXCHANGES}
    latest = max(dates.values())
    cutoff = min(dates.values())
    lagging = {exchange: day for exchange, day in dates.items() if day < latest}
    return CoverageCutoff(cutoff=cutoff, latest=latest, lagging=lagging)


def main(
    argv: list[str] | None = None,
    *,
    loader: Callable[..., Mapping[str, date]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="cta_carry.coverage",
        description="print the last trade_date every commodity exchange has reached",
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    if loader is None:
        from .pg_source import load_public_exchange_coverage

        loader = load_public_exchange_coverage
    since = args.as_of - timedelta(days=args.lookback_days)
    report = coverage_cutoff(loader(config_path=args.config, since=since))

    print(
        f"coverage as of {args.as_of}: latest={report.latest} cutoff={report.cutoff}",
        file=sys.stderr,
    )
    for exchange, day in sorted(report.lagging.items()):
        print(f"  lagging {exchange}: max trade_date {day}", file=sys.stderr)
    print(report.cutoff.isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
