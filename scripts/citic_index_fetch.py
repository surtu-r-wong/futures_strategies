"""Pull the replica's input and prove the dump is what the database holds.

    .venv/bin/python scripts/citic_index_fetch.py --out data/citic_index

Two files land in `--out`: `prices.csv`, the daily bars the replica reads, and
`official.csv`, the published index series to compare against.

`--all-products` pulls every commodity product rather than the thirty-seven 3.2
names.  The names are only "指数测试考虑" examples; 3.2's actual universe is
whatever clears its liquidity and listing filters, which reached sixty-nine
products by 2025.  The financial exchange is always excluded -- index and bond
futures are not commodities.

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

# The predicate is assembled in Python rather than left as a runtime OR.
# `(%(products)s IS NULL OR ...)` is not sargable: it turned a bounded range
# scan into a full one, 135 seconds for a single month against 0.3 once the
# clause is simply absent.
def _predicate(products) -> str:
    clauses = [
        "trade_date >= %(lo)s",
        "trade_date <= %(hi)s",
        # Index and bond futures are not commodities.
        "split_part(symbol, '.', 2) <> 'CFE'",
    ]
    if products is not None:
        clauses.append(_PRODUCT_EXPRESSION + " = ANY(%(products)s)")
    return "\n      AND ".join(clauses)


def _params(lo, hi, products) -> dict:
    params = {"lo": lo, "hi": hi}
    if products is not None:
        params["products"] = products
    return params


def _price_sql(products) -> str:
    return f"""
        SELECT
            trade_date,
            symbol AS contract,
            open::float AS open,
            high::float AS high,
            low::float AS low,
            close::float AS close,
            volume::float AS volume,
            oi::float AS oi,
            turnover::float AS turnover,
            settle::float AS settle
        FROM public.futures_daily
        WHERE {_predicate(products)}
        ORDER BY trade_date, symbol
    """


def _count_sql(products) -> str:
    return f"""
        SELECT to_char(trade_date, 'YYYY-MM') AS month, COUNT(*) AS n
        FROM public.futures_daily
        WHERE {_predicate(products)}
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
    parser.add_argument("--all-products", action="store_true",
                        help="every commodity product, not just the named 37")
    parser.add_argument("--extra-products", default=None)
    args = parser.parse_args(argv)

    dsn = _dsn(args.config)
    # 3.2's liquidity and listing filters are the actual universe; the
    # thirty-seven are only "指数测试考虑" examples.  Pull every commodity
    # product so the open-universe arm has something to open onto.
    products = sorted(NAMED_37 | set(args.extra_products.split(",") if args.extra_products else []))
    if args.all_products:
        products = None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    frames = []
    for lo, hi in date_chunks(args.start, args.end, chunk_days=args.chunk_days):
        started = time.time()
        frame, attempt = _query(
            dsn,
            _price_sql(products),
            _params(lo, hi, products),
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
        _count_sql(products),
        _params(args.start, args.end, products),
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
