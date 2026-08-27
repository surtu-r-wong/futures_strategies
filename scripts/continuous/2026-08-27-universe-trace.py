"""把成交额宇宙逐月铺开，供人工核对（计划 Task 1 Step 5）。

`futures_daily` 的 `turnover` 是 numeric，`read_sql` 会把它变成 Decimal 再撑爆内存
（[[pandas-pg-memory-pitfalls]]）。所以走 COPY 到本地 CSV 再让 pandas 按 float64 读。
"""

from __future__ import annotations

import io
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.config import load_config, resolve_settings_path  # noqa: E402
from common.db import get_connection, pg_config_from  # noqa: E402
from cta_continuous.universe import product_daily_turnover, universe_for_month  # noqa: E402

FIRST_MONTH = date(2012, 1, 1)
LAST_MONTH = date(2026, 1, 1)
RAW = Path("output/continuous/futures_daily_turnover.csv")
OUT = Path("output/continuous/universe_trace.csv")


def _months(first: date, last: date):
    cursor = first
    while cursor <= last:
        yield cursor
        cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)


def fetch() -> pd.DataFrame:
    if RAW.exists():
        print(f"复用 {RAW}")
    else:
        cfg = load_config(resolve_settings_path())
        buffer = io.StringIO()
        with get_connection(pg_config_from(cfg)) as conn, conn.cursor() as cur:
            cur.execute("SET statement_timeout='600s'")
            cur.copy_expert(
                "COPY (SELECT symbol, trade_date, turnover FROM public.futures_daily "
                "WHERE trade_date >= DATE '2011-07-01' AND trade_date < DATE '2026-02-01' "
                "AND turnover IS NOT NULL) TO STDOUT WITH CSV HEADER",
                buffer,
            )
        RAW.write_text(buffer.getvalue())
        print(f"写出 {RAW}")
    frame = pd.read_csv(RAW, dtype={"symbol": "string", "turnover": "float64"})
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame


def main() -> int:
    daily = fetch()
    print(f"日线行数 {len(daily):,}")
    turnover = product_daily_turnover(daily)
    print(f"品种-日 {len(turnover):,}，品种 {turnover['product'].nunique()}")

    rows = []
    previous: tuple[str, ...] = ()
    for month in _months(FIRST_MONTH, LAST_MONTH):
        picked = universe_for_month(turnover, month_start=month)
        rows.append(
            {
                "month": month.isoformat(),
                "count": len(picked),
                "entered": " ".join(sorted(set(picked) - set(previous))),
                "left": " ".join(sorted(set(previous) - set(picked))),
                "products": " ".join(picked),
            }
        )
        previous = picked
    trace = pd.DataFrame(rows)
    trace.to_csv(OUT, index=False)
    print(f"写出 {OUT}")
    print(trace.loc[:, ["month", "count", "entered", "left"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
