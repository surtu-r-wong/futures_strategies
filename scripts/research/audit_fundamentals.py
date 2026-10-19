"""Read-only, version-pinned fundamental quality inventory; exports no source values.

Run with PYTHONPATH=. and a 2 GB virtual-memory limit. No Wind calls, database
writes, builds or production job invocations are performed. All queries share
one repeatable-read snapshot and use the consumer's latest-complete selection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

from common.config import load_config, resolve_settings_path


PRODUCTS = ["AL", "BU", "CU", "M", "MA", "PP", "RB", "RU", "TA"]
LATEST = """
SELECT build_version,catalog_version,config_hash,formula_version,formula_hash,
       source_recorded_cutoff,finished_at,output_rows,absence_slices
FROM commodity_research.fundamental_build
WHERE status='complete' AND pit_mode='conservative'
ORDER BY finished_at DESC LIMIT 1
"""
QUERIES = {
    "build_integrity": """
SELECT min(trade_date) AS first_date,max(trade_date) AS last_date,count(*) AS rows,
       count(DISTINCT trade_date) AS dates,count(DISTINCT product_code) AS products,
       count(*)-count(DISTINCT (trade_date,product_code,metric)) AS duplicate_keys,
       count(*) FILTER (WHERE value IS NULL OR value::text IN ('NaN','Infinity','-Infinity')) AS nonfinite,
       count(*) FILTER (WHERE available_at IS NULL OR available_at >
          ((trade_date::timestamp + time '15:00') AT TIME ZONE 'Asia/Shanghai')) AS pit_violations,
       count(*) FILTER (WHERE catalog_version IS DISTINCT FROM %(catalog)s) AS catalog_mismatch,
       count(*) FILTER (WHERE staleness_trading_days IS NULL OR staleness_trading_days < 0) AS invalid_age
FROM commodity_research.fundamental_daily WHERE build_version=%(build)s
""",
    "catalog": """
SELECT series_id,product_code,metric_role,source_code,source_name,frequency,
       source_unit,target_unit,release_lag_rule,max_staleness_trading_days,
       valid_from,valid_to,config_hash
FROM commodity_research.series_catalog
WHERE catalog_version=%(catalog)s AND active ORDER BY product_code,metric_role,series_id
""",
    "source_history_at_build_cutoff": """
WITH obs AS (
 SELECT DISTINCT ON (o.series_id,o.observation_date)
        o.series_id,o.observation_date,o.raw_value,o.source_unit,o.currency,
        o.available_at,o.vintage_quality
 FROM commodity_research.fundamental_observation o
 JOIN commodity_research.series_catalog c USING(catalog_version,series_id)
 WHERE o.catalog_version=%(catalog)s AND c.active AND o.recorded_at<=%(cutoff)s
 ORDER BY o.series_id,o.observation_date,o.recorded_at DESC,o.id DESC
), gaps AS (
 SELECT *,observation_date-lag(observation_date) OVER
           (PARTITION BY series_id ORDER BY observation_date) AS gap_days FROM obs
)
SELECT c.series_id,min(g.observation_date) AS first_observation,
       max(g.observation_date) AS last_observation,count(g.observation_date) AS observations,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY g.gap_days) AS median_gap_days,
       max(g.gap_days) AS max_gap_days,
       count(*) FILTER (WHERE g.observation_date IS NOT NULL AND
          (g.raw_value IS NULL OR g.raw_value::text IN ('NaN','Infinity','-Infinity'))) AS nonfinite,
       count(*) FILTER (WHERE g.raw_value<0) AS negative_values,
       count(*) FILTER (WHERE g.observation_date IS NOT NULL AND
          (g.source_unit IS DISTINCT FROM c.source_unit OR g.currency IS DISTINCT FROM c.currency)) AS unit_currency_mismatch,
       count(*) FILTER (WHERE g.available_at IS NULL AND g.observation_date IS NOT NULL) AS missing_available_at
FROM commodity_research.series_catalog c LEFT JOIN gaps g USING(series_id)
WHERE c.catalog_version=%(catalog)s AND c.active
GROUP BY c.series_id ORDER BY c.series_id
""",
    "coverage_by_product_metric": """
WITH calendar AS (
 SELECT base_symbol AS product_code,count(DISTINCT trade_date) AS expected_dates
 FROM public.continuous_contract_ohlc
 WHERE rule_type='standard' AND base_symbol=ANY(%(products)s)
 AND trade_date BETWEEN %(first)s AND %(last)s GROUP BY base_symbol
), grid AS (
 SELECT * FROM calendar CROSS JOIN
 (VALUES ('spot'),('basis_rate'),('inventory'),('profit')) AS m(metric)
)
SELECT g.product_code,g.metric,g.expected_dates,count(d.trade_date) AS available_dates,
       min(d.trade_date) AS first_date,max(d.trade_date) AS last_date,
       max(d.staleness_trading_days) AS max_staleness
FROM grid g LEFT JOIN commodity_research.fundamental_daily d
 ON d.build_version=%(build)s AND d.product_code=g.product_code AND d.metric=g.metric
GROUP BY g.product_code,g.metric,g.expected_dates ORDER BY g.product_code,g.metric
""",
    "annual_coverage": """
SELECT extract(year FROM trade_date)::int AS year,metric,product_code,count(*) AS dates
FROM commodity_research.fundamental_daily WHERE build_version=%(build)s
GROUP BY 1,2,3 ORDER BY 1,2,3
""",
    "daily_floor_exceptions": """
WITH calendar AS (
 SELECT DISTINCT trade_date FROM public.continuous_contract_ohlc
 WHERE rule_type='standard' AND base_symbol=ANY(%(products)s)
 AND trade_date BETWEEN %(first)s AND %(last)s
), grid AS (
 SELECT * FROM calendar CROSS JOIN
 (VALUES ('basis_rate'),('inventory'),('profit')) AS m(metric)
)
SELECT g.trade_date,g.metric,count(DISTINCT d.product_code) AS products
FROM grid g LEFT JOIN commodity_research.fundamental_daily d
 ON d.build_version=%(build)s AND d.trade_date=g.trade_date AND d.metric=g.metric
 AND d.product_code=ANY(%(products)s) AND d.value IS NOT NULL
 AND d.value::text NOT IN ('NaN','Infinity','-Infinity')
GROUP BY g.trade_date,g.metric HAVING count(DISTINCT d.product_code)<6 ORDER BY 1,2
""",
    "direct_staleness_violations": """
SELECT d.product_code,d.metric,count(*) AS rows
FROM commodity_research.fundamental_daily d
JOIN commodity_research.series_catalog c USING(catalog_version,series_id)
WHERE d.build_version=%(build)s AND d.staleness_trading_days>c.max_staleness_trading_days
GROUP BY 1,2 ORDER BY 1,2
""",
    "latest_product_metrics": """
SELECT product_code,metric,source_observation_date,available_at,staleness_trading_days,
       series_id,formula_id,vintage_quality
FROM commodity_research.fundamental_daily WHERE build_version=%(build)s AND trade_date=%(last)s
ORDER BY product_code,metric
""",
    "recent_price_coverage": """
SELECT base_symbol,min(trade_date) AS first_date,max(trade_date) AS last_date,
       count(DISTINCT trade_date) AS dates,
       count(*) FILTER (WHERE close_raw IS NULL OR close_raw<=0 OR
                       close_raw::text IN ('NaN','Infinity','-Infinity')) AS bad_close
FROM public.continuous_contract_ohlc WHERE rule_type='standard'
AND base_symbol=ANY(%(products)s) AND trade_date BETWEEN %(last)s::date-30 AND %(as_of)s
GROUP BY base_symbol ORDER BY base_symbol
""",
}


def collect(connection):
    connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
    out = {"checked_at": datetime.now(timezone.utc).isoformat(),
           "read_only": True, "wind_calls": 0, "production_writes": 0,
           "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SET LOCAL statement_timeout='20s'")
            cursor.execute("SET LOCAL lock_timeout='2s'")
            cursor.execute("SET LOCAL work_mem='16MB'")
            cursor.execute("SET LOCAL idle_in_transaction_session_timeout='30s'")
            cursor.execute(LATEST)
            build = cursor.fetchone()
            if not build:
                raise ValueError("no complete conservative build")
            out["build"] = dict(build)
            params = {"build": build["build_version"], "catalog": build["catalog_version"],
                      "cutoff": build["source_recorded_cutoff"], "products": PRODUCTS,
                      "as_of": datetime.now(timezone.utc).date()}
            out["results"] = {}
            for name, query in QUERIES.items():
                cursor.execute(query, params)
                rows = [dict(row) for row in cursor.fetchall()]
                out["results"][name] = rows
                if name == "build_integrity":
                    params.update(first=rows[0]["first_date"], last=rows[0]["last_date"])
            waived = {(a["trade_date"], a["metric"]) for a in build["absence_slices"] or []}
            failures = [row for row in out["results"]["daily_floor_exceptions"]
                        if (str(row["trade_date"]), row["metric"]) not in waived]
            integrity = out["results"]["build_integrity"][0]
            source_rows = out["results"]["source_history_at_build_cutoff"]
            roles = {row["series_id"]: row["metric_role"] for row in out["results"]["catalog"]}
            out["checks"] = {
                "output_rows_match": integrity["rows"] == build["output_rows"],
                "integrity_zero": all(integrity[k] == 0 for k in
                    ("duplicate_keys", "nonfinite", "pit_violations", "catalog_mismatch", "invalid_age")),
                "catalog_hash_match": bool(out["results"]["catalog"]) and all(
                    row["config_hash"] == build["config_hash"] for row in out["results"]["catalog"]),
                "unwaived_daily_floor_failures": failures,
                "direct_staleness_pass": not out["results"]["direct_staleness_violations"],
                "source_shape_pass": all(row["observations"] > 0 and all(row[k] == 0 for k in
                    ("nonfinite", "unit_currency_mismatch", "missing_available_at")) for row in source_rows),
                # A negative profit is valid; a negative physical inventory is not.
                "inventory_nonnegative": all(row["negative_values"] == 0 for row in source_rows
                    if roles[row["series_id"]] == "inventory"),
            }
        return out
    finally:
        connection.rollback()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cfg = load_config(resolve_settings_path())["database"]
    connection = None
    try:
        connection = psycopg2.connect(host=cfg["host"], port=cfg["port"], dbname=cfg["name"],
                                     user=cfg["user"], password=cfg["password"], connect_timeout=8)
        result = collect(connection)
    except Exception as exc:
        # Connection exceptions can contain credentials/DSNs; never print them.
        print(f"Fundamental audit failed ({type(exc).__name__}); no report written.")
        return 1
    finally:
        if connection is not None:
            connection.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({"build": result["build"]["build_version"], "checks": result["checks"],
                      "output": str(args.output)}, ensure_ascii=False, default=str))
    checks = result["checks"]
    return 0 if (checks["output_rows_match"] and checks["integrity_zero"] and
                 checks["catalog_hash_match"] and checks["direct_staleness_pass"] and
                 checks["source_shape_pass"] and checks["inventory_nonnegative"] and
                 not checks["unwaived_daily_floor_failures"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
