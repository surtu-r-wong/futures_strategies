"""Live bars for the daily run, pulled the way the relay can survive.

The same date-sliced pull the fetch script uses, with the same reconciliation
gate: this machine reaches the database over a DERP relay that serves a
half-second query without trouble and drops a multi-minute one about half the
time, so a single window-sized fetch is the shape that reliably fails here.

Bars go through `cta_carry`'s normaliser, so a contract code cannot mean two
things in one repo -- Zhengzhou's single-digit delivery year in particular only
resolves against the trade date.
"""

import time
from datetime import date

import pandas as pd
import psycopg2

from common.config import load_config, resolve_settings_path
from common.db import pg_config_from
from cta_carry.data import normalize_contract_daily

from citic_index.data import date_chunks, monthly_counts, reconcile


# The same expression the production public-pg path uses.
_PRODUCT_EXPRESSION = "UPPER(substring(symbol from '^[A-Za-z]+'))"

_COLUMNS = """
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
"""

# Index and bond futures are not commodities; 3.2's liquidity and listing gates
# decide the rest.
_WHERE = """
    trade_date >= %(lo)s
      AND trade_date <= %(hi)s
      AND split_part(symbol, '.', 2) <> 'CFE'
"""


def _dsn(config_path=None) -> dict:
    pg = pg_config_from(load_config(config_path or resolve_settings_path()))
    return dict(
        host=pg["host"], port=pg["port"], dbname=pg["name"],
        user=pg["user"], password=pg["password"],
        connect_timeout=45, keepalives=1, keepalives_idle=15,
        keepalives_interval=5, keepalives_count=5,
    )


def _query(dsn, sql, params, *, attempts, timeout):
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
            return pd.DataFrame(rows, columns=columns)
        except Exception as exc:  # noqa: BLE001 -- the link, not the query
            last = exc
            print(f"    attempt {attempt} failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(10)
    raise RuntimeError(f"gave up after {attempts} attempts: {last}")


def load_window(
    *,
    start: date,
    end: date,
    config_path=None,
    chunk_days: int = 60,
    attempts: int = 6,
    verbose: bool = True,
) -> pd.DataFrame:
    """Normalised commodity bars over [start, end], reconciled against the source."""
    dsn = _dsn(config_path)
    frames = []
    for lo, hi in date_chunks(start, end, chunk_days=chunk_days):
        began = time.time()
        frame = _query(
            dsn,
            f"SELECT {_COLUMNS} FROM public.futures_daily WHERE {_WHERE}"
            " ORDER BY trade_date, symbol",
            {"lo": lo, "hi": hi},
            attempts=attempts,
            timeout="180s",
        )
        if verbose:
            print(
                f"  {lo}..{hi}  {len(frame):>7} rows  {time.time() - began:5.1f}s",
                flush=True,
            )
        frames.append(frame)

    raw = pd.concat(frames, ignore_index=True).drop_duplicates()
    counts = _query(
        dsn,
        "SELECT to_char(trade_date, 'YYYY-MM') AS month, COUNT(*) AS n"
        f" FROM public.futures_daily WHERE {_WHERE} GROUP BY 1",
        {"lo": start, "hi": end},
        attempts=attempts,
        timeout="300s",
    )
    remote = counts.set_index("month")["n"].astype(int).sort_index()
    mismatch = reconcile(monthly_counts(raw), remote)
    if not mismatch.empty:
        raise RuntimeError(
            "the window does not match the database:\n"
            + mismatch.to_string(index=False)
        )
    if verbose:
        print(
            f"  reconciled {len(remote)} months, {int(remote.sum())} rows,"
            " zero difference",
            flush=True,
        )
    return normalize_contract_daily(raw).prices
