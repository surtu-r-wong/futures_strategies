"""Pull the replica's input and prove the dump is what the database holds.

    .venv/bin/python scripts/citic_index_fetch.py --out data/citic_index

Two files land in `--out`: `prices.csv`, the daily bars of the thirty-seven
products CITIC 3.2 names, and `official.csv`, the published index series to
compare against.

The pull is sliced by date and each slice gets its own short connection with
its own retries.  This machine reaches the database over a DERP relay that
serves a half-second query without trouble and drops a multi-minute one about
half the time, so a single large fetch is the one shape that reliably fails.

The run then counts rows per calendar month on both sides of the same
predicate and exits non-zero on any disagreement.  An unreconciled bundle is
not allowed to produce a published number.
"""

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from citic_index.data import date_chunks, monthly_counts, reconcile  # noqa: E402
from citic_index.universe import NAMED_37  # noqa: E402
from common.config import load_config, resolve_settings_path  # noqa: E402
from common.db import pg_config_from  # noqa: E402


# Same expression the production public-pg path uses, so "which product is this
# contract" cannot mean two things in one repo.
_PRODUCT_EXPRESSION = "UPPER(substring(symbol from '^[A-Za-z]+'))"

_PRICE_SQL = f"""
    SELECT
        trade_date,
        symbol AS contract,
        open::float AS open,
        high::float AS high,
        low::float AS low,
        close::float AS close,
        volume::float AS volume,
        oi::float AS oi,
        turnover::float AS turnover
    FROM public.futures_daily
    WHERE trade_date >= %(lo)s
      AND trade_date <= %(hi)s
      AND {_PRODUCT_EXPRESSION} = ANY(%(products)s)
    ORDER BY trade_date, symbol
"""

_COUNT_SQL = f"""
    SELECT to_char(trade_date, 'YYYY-MM') AS month, COUNT(*) AS n
    FROM public.futures_daily
    WHERE trade_date >= %(lo)s
      AND trade_date <= %(hi)s
      AND {_PRODUCT_EXPRESSION} = ANY(%(products)s)
    GROUP BY 1
"""

_OFFICIAL_SQL = """
    SELECT index_code, trade_date, close::float AS close
    FROM stock_selector.index_daily
    WHERE index_code = ANY(%(codes)s)
    ORDER BY index_code, trade_date
"""

OFFICIAL_CODES = ["CICSF027.WI", "CICSF025.WI", "CICSF023.WI", "CICSF026.WI"]


def _dsn(config_path=None) -> dict:
    settings = load_config(config_path or resolve_settings_path())
    pg = pg_config_from(settings)
    return dict(
        host=pg["host"],
        port=pg["port"],
        dbname=pg["name"],
        user=pg["user"],
        password=pg["password"],
        connect_timeout=45,
        keepalives=1,
        keepalives_idle=15,
        keepalives_interval=5,
        keepalives_count=5,
    )


def _query(dsn: dict, sql: str, params: dict, *, attempts: int, timeout: str):
    """One short connection per attempt; a dropped relay costs one slice."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            conn = psycopg2.connect(**dsn)
            try:
                cur = conn.cursor()
                cur.execute(f"SET statement_timeout = '{timeout}'")
                cur.execute(sql, params)
                columns = [d[0] for d in cur.description]
                rows = cur.fetchall()
            finally:
                conn.close()
            return pd.DataFrame(rows, columns=columns), attempt
        except Exception as exc:  # noqa: BLE001 -- the link, not the query
            last = exc
            print(f"    attempt {attempt} failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(10)
    raise SystemExit(f"gave up after {attempts} attempts: {last}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="citic_index_fetch")
    parser.add_argument("--out", default="data/citic_index")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2009, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--chunk-days", type=int, default=60)
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    dsn = _dsn(args.config)
    products = sorted(NAMED_37)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    frames = []
    for lo, hi in date_chunks(args.start, args.end, chunk_days=args.chunk_days):
        started = time.time()
        frame, attempt = _query(
            dsn,
            _PRICE_SQL,
            {"lo": lo, "hi": hi, "products": products},
            attempts=args.attempts,
            timeout="180s",
        )
        print(
            f"{lo}..{hi}  {len(frame):>7} rows  {time.time() - started:5.1f}s"
            f"  (attempt {attempt})",
            flush=True,
        )
        frames.append(frame)

    prices = pd.concat(frames, ignore_index=True).drop_duplicates()
    print(f"\n{len(prices)} rows, {prices['trade_date'].min()}..{prices['trade_date'].max()}")

    counts, _ = _query(
        dsn,
        _COUNT_SQL,
        {"lo": args.start, "hi": args.end, "products": products},
        attempts=args.attempts,
        timeout="300s",
    )
    remote = counts.set_index("month")["n"].astype(int).sort_index()
    mismatch = reconcile(monthly_counts(prices), remote)
    if not mismatch.empty:
        print("\nRECONCILIATION FAILED -- the dump is not what the database holds:")
        print(mismatch.to_string(index=False))
        return 3
    print(f"reconciled: {len(remote)} months, {int(remote.sum())} rows, zero difference")

    prices.to_csv(out / "prices.csv", index=False)
    print(f"wrote {out / 'prices.csv'}  ({(out / 'prices.csv').stat().st_size / 1e6:.1f} MB)")

    official, _ = _query(
        dsn,
        _OFFICIAL_SQL,
        {"codes": OFFICIAL_CODES},
        attempts=args.attempts,
        timeout="120s",
    )
    official.to_csv(out / "official.csv", index=False)
    for code, group in official.groupby("index_code"):
        print(
            f"  {code}: {len(group)} rows,"
            f" {group['trade_date'].min()}..{group['trade_date'].max()}"
        )
    print(f"wrote {out / 'official.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
