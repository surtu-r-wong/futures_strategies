"""Pull 023's factor input from the exchanges' own reports instead of Wind.

    .venv/bin/python scripts/citic_receipts_exchange_fetch.py --out data/citic_023

Writes `receipts_exchange.csv` -- product, trade_date, receipts, forecast --
beside the Wind `receipts.csv` the replica has been reading, plus an audit
JSON.  Nothing is overwritten: the two files are two measurements of the same
quantity and the point is to compare them.

Why bother, when `commodity_research` already catalogues 66 receipt series:
**only CZCE publishes 有效预报**, the tonnage declared for warranting but not
yet warranted, and the Wind series carry the warrants alone.  Whether the
declared tonnage belongs in a receipt-momentum factor is an empirical
question that cannot be asked off the series we have.

The archive is raw -- the office's own disposition says it "不能当已入库/可加总
库存" -- so the reading of each exchange's shape lives in
`citic_index.exchange_receipts`, which is tested against the shapes seen here.
This script only slices the pull and proves what it wrote.

Each column is summed server-side.  SHFE alone has 1.9M warehouse rows and
the link to this database is a relay that drops long queries, so the pull is
month-sliced with its own short connection per slice, the same shape
`citic_index_fetch.py` settled on.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from citic_index.data import date_chunks  # noqa: E402
from citic_index.exchange_receipts import (  # noqa: E402
    FORECAST_METRICS,
    RECEIPT_METRICS,
    TOTAL_ROW_KIND,
    normalise_records,
)
from common.config import load_config, resolve_settings_path  # noqa: E402
from common.db import pg_config_from  # noqa: E402

METRICS = sorted(RECEIPT_METRICS | FORECAST_METRICS)

# SHFE is not in the default pull, and the reason is worth stating rather than
# leaving as an absence.  Its detail rows change shape twice: the 2008-2014
# report labels the product in `product_label` and the quantity in 期货, the
# 2014-05-19..2025-11-17 JSON API leaves the label empty and carries the
# product in `dimensions.VARNAME` with the quantity in `WRTWGHTS` (688,261
# rows, the bulk of the history), and the current report goes back to labels
# but splits 仓库/厂库.  Three parsers, and SHFE publishes no 有效预报 -- the
# whole reason for reading the archive -- so switching it off Wind adds a
# rewrite's worth of new failure modes and no new information.  The factor is
# a product against its own 200-day-old baseline, so which source a *different*
# product came from cannot move a rank; only a break inside one product's own
# series can.  Pass --sources to include it once its eras are implemented.
DEFAULT_SOURCES = ["CZCE_TEXT", "CZCE_LEGACY", "DCE_WARRANTS", "GFEX_WARRANTS"]

# One row per product-day, source row class and column, already summed.
#
# The numbers do not live in the same place in every capture: CZCE and SHFE
# put them in `payload.numeric_values`, DCE and GFEX put them at the top of
# the payload (`current_quantity`).  Reading only the nested object returned
# CZCE alone and dropped two exchanges without erroring -- the metric
# whitelist below is what makes the COALESCE safe.
#
# The cast is guarded: the archive keeps every cell as the string the exchange
# wrote, and a blank is not a zero -- `normalise_records` needs to see the
# difference, so a non-numeric cell contributes NULL rather than 0.
_LONG_SQL = """
    SELECT rec.source_kind,
           COALESCE(rec.report_date, rec.requested_date) AS trade_date,
           COALESCE(rec.product_code, '') AS product_code,
           COALESCE(rec.product_label, '') AS product_label,
           rec.row_kind,
           kv.key AS metric,
           -- Kept per row everywhere but SHFE, whose 1.9M warehouse lines
           -- have to be summed in the server.  The ordinal is what tells a
           -- section total from the grand total that follows it.
           CASE WHEN rec.source_kind LIKE 'SHFE%%' THEN NULL
                ELSE rec.record_ordinal END AS record_ordinal,
           SUM(CASE WHEN kv.value ~ '^-?[0-9]+(\\.[0-9]+)?$'
                    THEN kv.value::numeric END)::float AS value,
           COUNT(*) FILTER (
               WHERE kv.value ~ '^-?[0-9]+(\\.[0-9]+)?$'
           ) AS n_rows
    FROM exchange_data.warehouse_source_record_latest rec
    CROSS JOIN LATERAL jsonb_each_text(
        COALESCE(rec.payload->'numeric_values', rec.payload)
    ) kv
    WHERE rec.requested_date >= %(lo)s
      AND rec.requested_date <= %(hi)s
      AND rec.source_kind = ANY(%(sources)s)
      AND rec.row_kind = ANY(%(row_kinds)s)
      AND kv.key = ANY(%(metrics)s)
    GROUP BY 1, 2, 3, 4, 5, 6, 7
"""

# SHFE is summed from its warehouse rows because 黄金 occupies one warehouse
# and gets no total row.  Where the exchange did print a total, it is an
# independent statement of the same number, so it is pulled and reconciled.
_SHFE_TOTAL_SQL = """
    SELECT COALESCE(rec.report_date, rec.requested_date) AS trade_date,
           rec.product_label,
           SUM(CASE WHEN kv.value ~ '^-?[0-9]+(\\.[0-9]+)?$'
                    THEN kv.value::numeric END)::float AS stated_total
    FROM exchange_data.warehouse_source_record_latest rec
    CROSS JOIN LATERAL jsonb_each_text(rec.payload->'numeric_values') kv
    WHERE rec.requested_date >= %(lo)s
      AND rec.requested_date <= %(hi)s
      AND rec.source_kind = 'SHFE_DAILY'
      AND rec.row_kind = 'EXPLICIT_SUMMARY'
      AND rec.payload->'dimensions'->>'地区' = '总计'
      AND kv.key = '期货'
    GROUP BY 1, 2
"""


# Which sources actually published inside the window.  A source with no
# captures here is not missing, it just had not started yet (GFEX) or had
# already been replaced (CZCE_LEGACY).
_EXPECTED_SQL = """
    SELECT DISTINCT source_kind
    FROM exchange_data.warehouse_source_capture
    WHERE requested_date >= %(lo)s
      AND requested_date <= %(hi)s
      AND source_kind = ANY(%(sources)s)
      AND availability IN ('AVAILABLE', 'SUMMARY_ONLY')
      AND parsed_rows > 0
"""


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
    last = None
    for attempt in range(1, attempts + 1):
        try:
            conn = psycopg2.connect(**dsn)
            try:
                cur = conn.cursor()
                cur.execute(f"SET statement_timeout = '{timeout}'")
                # The primary has 15 GB and runs the collectors; this pull is
                # never worth a parallel worker.
                cur.execute("SET max_parallel_workers_per_gather = 0")
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
    parser = argparse.ArgumentParser(prog="citic_receipts_exchange_fetch")
    parser.add_argument("--out", default="data/citic_023")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2008, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--chunk-days", type=int, default=120)
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--config", default=None)
    parser.add_argument("--filename", default="receipts_exchange.csv")
    parser.add_argument(
        "--sources",
        default=",".join(DEFAULT_SOURCES),
        help="capture kinds to read; SHFE_DAILY needs its three eras first",
    )
    args = parser.parse_args(argv)

    sources = sorted({s.strip() for s in args.sources.split(",") if s.strip()})
    unknown = sorted(set(sources) - set(TOTAL_ROW_KIND))
    if unknown:
        raise SystemExit(f"no reading implemented for {unknown}")
    left_out = sorted(set(TOTAL_ROW_KIND) - set(sources))
    print(f"reading {sources}")
    if left_out:
        print(f"  NOT reading {left_out} -- those products keep whatever source they had")

    dsn = _dsn(args.config)
    row_kinds = sorted({TOTAL_ROW_KIND[s] for s in sources})

    frames, totals = [], []
    for lo, hi in date_chunks(args.start, args.end, chunk_days=args.chunk_days):
        started = time.time()
        frame, attempt = _query(
            dsn,
            _LONG_SQL,
            {"lo": lo, "hi": hi, "sources": sources,
             "row_kinds": row_kinds, "metrics": METRICS},
            attempts=args.attempts,
            timeout="300s",
        )
        stated = pd.DataFrame(columns=["trade_date", "product_label", "stated_total"])
        if "SHFE_DAILY" in sources:
            stated, _ = _query(
                dsn,
                _SHFE_TOTAL_SQL,
                {"lo": lo, "hi": hi},
                attempts=args.attempts,
                timeout="300s",
            )
        print(
            f"{lo}..{hi}  {len(frame):>7} rows  {time.time() - started:5.1f}s"
            f"  (attempt {attempt})",
            flush=True,
        )
        frames.append(frame)
        totals.append(stated)

    raw = pd.concat(frames, ignore_index=True)
    if raw.empty:
        print("the archive returned nothing for this window")
        return 3
    raw["trade_date"] = pd.to_datetime(raw["trade_date"]).dt.date

    # SHFE is keyed by its Chinese label, everyone else by the exchange's code.
    raw["product_key"] = raw["product_code"].where(
        ~raw["source_kind"].str.startswith("SHFE"), raw["product_label"]
    )
    # A source that has captures in this window and yet parsed to nothing is
    # the failure this pull already made once: DCE and GFEX keep their numbers
    # at the top of the payload, the query read only the nested object, and
    # two exchanges went missing with every count still looking plausible.
    # The capture table says which sources the window should have produced,
    # so the check is against the archive rather than against a hard-coded
    # expectation that goes stale when an exchange starts or stops.
    expected, _ = _query(
        dsn,
        _EXPECTED_SQL,
        {"lo": args.start, "hi": args.end, "sources": sources},
        attempts=args.attempts,
        timeout="120s",
    )
    have = set(raw["source_kind"].unique())
    silent = sorted(set(expected["source_kind"]) - have)
    if silent:
        raise SystemExit(
            f"{silent} have captures in this window and parsed to no rows."
            " The payload does not keep its numbers where this query looks."
        )
    print(f"  captures present for {sorted(expected['source_kind'])}")

    receipts, report = normalise_records(
        raw.loc[:, ["source_kind", "trade_date", "product_key", "row_kind",
                    "metric", "value", "n_rows", "record_ordinal"]]
    )
    per_source = raw.groupby("source_kind").size().to_dict()
    print(f"  rows by source: {per_source}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / args.filename
    receipts.to_csv(path, index=False)

    stated = pd.concat(totals, ignore_index=True)
    audit = _audit(raw, receipts, stated, report)
    audit["sources_read"] = sources
    audit["sources_left_out"] = left_out
    audit_path = path.with_suffix(".audit.json")
    audit_path.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")

    print(
        f"\nwrote {path}: {len(receipts):,} product-days,"
        f" {receipts['product'].nunique()} products,"
        f" {receipts['trade_date'].min()}..{receipts['trade_date'].max()}"
    )
    print(
        f"  forecast is non-zero on {int((receipts['forecast'] > 0).sum()):,}"
        f" product-days across {receipts.loc[receipts['forecast'] > 0, 'product'].nunique()}"
        f" products"
    )
    print(f"  {report}")
    print(f"  SHFE stated-total check: {audit['shfe_reconciliation']}")
    print(f"wrote {audit_path}")
    return 0


def _audit(raw, receipts, stated, report) -> dict:
    """What the pull saw, and whether SHFE's own totals agree with our sum."""
    by_product = (
        receipts.groupby("product")
        .agg(
            days=("trade_date", "size"),
            first=("trade_date", "min"),
            last=("trade_date", "max"),
            zero_days=("receipts", lambda s: int((s == 0).sum())),
            forecast_days=("forecast", lambda s: int((s > 0).sum())),
            median_receipts=("receipts", "median"),
            median_forecast=("forecast", "median"),
        )
        .reset_index()
    )

    shfe = raw.loc[raw["source_kind"] == "SHFE_DAILY"]
    check = {"compared": 0, "mismatched": 0, "worst_abs_diff": 0.0}
    if not shfe.empty and not stated.empty:
        ours = (
            shfe.loc[shfe["metric"] == "期货"]
            .groupby(["trade_date", "product_label"], as_index=False)["value"]
            .sum()
        )
        stated = stated.copy()
        stated["trade_date"] = pd.to_datetime(stated["trade_date"]).dt.date
        merged = ours.merge(stated, on=["trade_date", "product_label"], how="inner")
        if not merged.empty:
            diff = (merged["value"] - merged["stated_total"]).abs()
            check = {
                "compared": int(len(merged)),
                "mismatched": int((diff > 1e-6).sum()),
                "worst_abs_diff": float(diff.max()),
            }
    return {
        "normalisation_report": report,
        "shfe_reconciliation": check,
        "products": by_product.to_dict(orient="records"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
