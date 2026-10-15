"""One current 027 target proposal, with native warmup and read-only source capture.

No capital, account initialization, order generation, historical performance export,
producer script invocation or database mutation is exposed by this entry point.
"""

import argparse
from dataclasses import replace
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import resource
import socket
import subprocess
import sys
import traceback

import numpy as np
import pandas as pd
import psycopg2

from citic_index.book import lever, rolling_vol_scale
from citic_index.daily import LOOKBACK_DAYS, PRODUCTION
from citic_index.data import date_chunks, monthly_counts, reconcile
from citic_index.pg_source import _COLUMNS, _WHERE, _dsn
from citic_index.pipeline import build_from_panel, build_panel
from cta_carry.coverage import COMMODITY_EXCHANGES, coverage_cutoff
from cta_carry.data import normalize_contract_daily
from forward_account.contracts import digest, now_iso

ROOT = Path(__file__).resolve().parents[1]
NATIVE_FILES = [
    f"citic_index/{name}.py"
    for name in (
        "__init__",
        "book",
        "daily",
        "pipeline",
        "universe",
        "factor",
        "returns",
        "legs",
        "weights",
        "index",
        "data",
        "pg_source",
    )
]
NATIVE_FILES += [
    f"cta_carry/{name}.py"
    for name in (
        "__init__",
        "legreturns",
        "data",
        "risk",
        "targets",
        "config",
        "curve",
        "signals",
    )
]
NATIVE_FILES += [
    "common/__init__.py",
    "common/config.py",
    "common/db.py",
    "scripts/citic027_daily.sh",
]


def write_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )


def file_sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_failure_trace(exc):
    """Positions only: never serialize exception text, source lines or locals."""
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__):
        path = Path(frame.filename).resolve()
        try:
            filename = str(path.relative_to(ROOT))
        except ValueError:
            filename = path.name
        frames.append({"file": filename, "line": frame.lineno, "function": frame.name})
    return frames


def source_files():
    return {
        name: file_sha(ROOT / name)
        for name in NATIVE_FILES
        + ["forward_inputs/m01.py", "forward_account/contracts.py"]
    }


def current_weights(result, signal_date, config):
    """Native daily calculation through lever(), then a capital-free projection.

    The dominant selection and weighting are identical to book.next_targets;
    its inferred multiplier, capital-dependent notional/lots are not exported.
    """
    returns = result.index.set_index("trade_date")["daily_return"]
    scale = rolling_vol_scale(
        returns,
        vol_window=config.vol_window,
        target_vol=config.target_vol,
        min_observations=config.vol_window,
    )
    if not np.isfinite(scale.get(signal_date, np.nan)) or scale.get(signal_date) <= 0:
        raise ValueError("native volatility window is incomplete")
    weights = result.weights.loc[result.weights["trade_date"] == signal_date]
    selected = lever(weights, scale, config=config)
    if selected.empty or selected["product"].duplicated().any():
        raise ValueError("empty or duplicate native weights")
    legs = result.legs.loc[
        result.legs["trade_date"] == signal_date,
        ["product", "main_contract", "main_close"],
    ]
    selected = selected.merge(legs, on="product", how="left", validate="one_to_one")
    if selected[["main_contract", "main_close", "target_weight"]].isna().any().any():
        raise ValueError("missing dominant contract or weight")
    if (
        selected["main_contract"].duplicated().any()
        or not np.isfinite(selected["target_weight"]).all()
    ):
        raise ValueError("duplicate contract or invalid weight")
    if (
        not np.isfinite(selected["main_close"]).all()
        or (selected["main_close"] <= 0).any()
    ):
        raise ValueError("invalid dominant reference close")
    selected = selected.rename(
        columns={
            "trade_date": "signal_date",
            "weight": "raw_weight",
            "main_contract": "contract",
            "main_close": "signal_close_reference",
        }
    )
    return selected.sort_values("product", kind="mergesort")[
        [
            "signal_date",
            "product",
            "contract",
            "raw_weight",
            "vol_scale",
            "target_weight",
            "signal_close_reference",
        ]
    ].reset_index(drop=True)


def scalar(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, (str, bool, int, float)):
        return value
    return str(value)  # Decimal source values preserve original precision/units.


def preview_evidence(targets, prices, metadata, *, observed_at):
    """Keep EOD references and raw contract fields; do not certify execution."""
    result = {
        "execution_status": "blocked",
        "daily_settlement_status": "not_adapted_not_an_m01_target_gate",
        "valid_execution_interval": None,
        "intended_execution_rule": "next_verified_trading_day_day_open_after_signal",
        "contracts": [],
    }
    for target in targets.to_dict("records"):
        contract, signal_date = target["contract"], target["signal_date"]
        bare = contract.split(".")[
            0
        ]  # source prices are already canonical four-digit contracts
        quote = prices.loc[
            (prices["contract"] == contract) & (prices["trade_date"] == signal_date)
        ]
        matches = (
            metadata.loc[
                (metadata["合约代码"].str.upper() == bare.upper())
                & (metadata["交易日期"] == signal_date)
            ]
            if not metadata.empty
            else metadata
        )
        blockers = [
            "missing_live_buy_sell_evidence",
            "missing_verified_execution_session",
            "missing_minimum_order_lots",
            "unverified_margin_units_and_effectivity",
            "unverified_fee_units_and_effectivity",
            "unverified_last_date_semantics",
        ]
        raw = {}
        if len(matches) == 1:
            raw = {k: scalar(v) for k, v in matches.iloc[0].items()}
        else:
            blockers.append(
                "missing_contract_metadata"
                if matches.empty
                else "conflicting_contract_metadata"
            )
        mult = raw.get("合约乘数")
        if type(mult) not in (int, float) or not np.isfinite(mult) or mult <= 0:
            mult = None
            blockers.append("missing_valid_metadata_multiplier")
        close = scalar(quote.iloc[0]["close"]) if len(quote) == 1 else None
        if close is None or not np.isfinite(float(close)) or float(close) <= 0:
            close = None
            blockers.append("missing_reference_price")
        result["contracts"].append(
            {
                "contract": contract,
                "reference_price": {
                    "kind": "eod_close_reference",
                    "value": close,
                    "data_date": str(signal_date),
                    "price_at": None,
                    "observed_at": observed_at,
                    "source_available_at": None,
                    "source": "public.futures_daily.close",
                },
                "tradable_buy": None,
                "tradable_sell": None,
                "specification": {
                    "source": "public.futures_contract_info",
                    "metadata_symbol": bare,
                    "data_date": raw.get("交易日期"),
                    "multiplier": mult,
                    "multiplier_effective_interval": None,
                    "margin_long_raw": raw.get("多头保证金率"),
                    "margin_short_raw": raw.get("空头保证金率"),
                    "margin_rate": None,
                    "minimum_order_lots": None,
                    "delivery_month_order_lots_raw": raw.get("交割月下单整手数"),
                    "fee_method_raw": raw.get("手续费收取方式"),
                    "open_fee_raw": raw.get("开仓手续费"),
                    "close_fee_raw": raw.get("平仓手续费"),
                    "intraday_open_fee_raw": raw.get("日内开仓手续费"),
                    "intraday_close_fee_raw": raw.get("日内平仓手续费"),
                    "last_date_raw": raw.get("最后日期"),
                    "minimum_price_raw": raw.get("最小价格"),
                },
                "metadata_raw": raw,
                "blockers": blockers,
            }
        )
    return result


class ReadOnlySource:
    """A single repeatable, read-only snapshot; failed queries are not retried."""

    def __init__(self):
        self.conn = None
        self.queries = []

    def __enter__(self):
        dsn = _dsn()
        dsn["connect_timeout"] = 10
        self.conn = psycopg2.connect(**dsn)
        try:
            self.conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
            with self.conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '45s'")
                cur.execute("SHOW transaction_read_only")
                if cur.fetchone()[0] != "on":
                    raise ValueError("read-only transaction required")
        except Exception:
            self.conn.close()
            raise
        return self

    def __exit__(self, *args):
        self.conn.rollback()
        self.conn.close()

    def query(self, sql, params):
        if not sql.lstrip().upper().startswith("SELECT "):
            raise ValueError("only bounded source SELECTs are allowed")
        started = now_iso()
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            frame = pd.DataFrame(
                cur.fetchall(), columns=[col[0] for col in cur.description]
            )
        self.queries.append(
            {
                "sql": sql,
                "params": {
                    k: scalar(v) if not isinstance(v, list) else v
                    for k, v in params.items()
                },
                "started_at": started,
                "observed_at": now_iso(),
                "rows": len(frame),
            }
        )
        return frame

    def frontier(self, as_of):
        return self.query(
            "SELECT split_part(symbol, '.', 2) AS exchange, max(trade_date) AS max_date "
            "FROM public.futures_daily WHERE trade_date >= %(lo)s AND trade_date <= %(hi)s "
            "AND split_part(symbol, '.', 2) = ANY(%(exchanges)s) GROUP BY 1 ORDER BY 1",
            {
                "lo": as_of - timedelta(days=30),
                "hi": as_of,
                "exchanges": list(COMMODITY_EXCHANGES),
            },
        )

    def window(self, start, end, root):
        frames = []
        for lo, hi in date_chunks(start, end, chunk_days=60):
            frames.append(
                self.query(
                    f"SELECT {_COLUMNS} FROM public.futures_daily WHERE {_WHERE} ORDER BY trade_date, symbol",
                    {"lo": lo, "hi": hi},
                )
            )
        raw = pd.concat(frames, ignore_index=True).drop_duplicates()
        del frames
        counts = self.query(
            "SELECT to_char(trade_date, 'YYYY-MM') AS month, COUNT(*) AS n "
            f"FROM public.futures_daily WHERE {_WHERE} GROUP BY 1",
            {"lo": start, "hi": end},
        )
        mismatch = reconcile(
            monthly_counts(raw), counts.set_index("month")["n"].astype(int).sort_index()
        )
        if not mismatch.empty:
            raise ValueError("source count reconciliation failed")
        # Raw captured inputs are local evidence, never a historical performance output.
        raw.to_csv(root / "source-daily.csv.gz", index=False, compression="gzip")
        return normalize_contract_daily(raw).prices

    def metadata(self, contracts, as_of):
        return self.query(
            'SELECT "交易日期", "合约代码", "交易所代码", "合约乘数", "最小价格", '
            '"最后日期", "多头保证金率", "空头保证金率", "涨停价", "跌停价", '
            '"手续费收取方式", "开仓手续费", "平仓手续费", "日内开仓手续费", "日内平仓手续费", '
            '"交割月下单整手数", "创建时间", "更新时间", updated_at '
            'FROM public.futures_contract_info WHERE "交易日期" = %(day)s '
            'AND upper("合约代码") = ANY(%(symbols)s) ORDER BY "合约代码"',
            {"day": as_of, "symbols": [c.split(".")[0].upper() for c in contracts]},
        )


def run(as_of, provenance_path, output_dir):
    limit, _ = resource.getrlimit(resource.RLIMIT_AS)
    if limit == resource.RLIM_INFINITY or limit > 2000000 * 1024:
        raise ValueError("run with a 2 GB virtual memory limit")
    provenance_path = Path(provenance_path).resolve()
    production = json.loads(provenance_path.read_bytes())
    before = source_files()
    if production.get("tracked_changes") or production.get("local_mismatches"):
        raise ValueError("production source changes require review")
    if any(
        production.get("files", {}).get(name) != before[name] for name in NATIVE_FILES
    ):
        raise ValueError("local calculation sources differ from observed production")
    if not production.get("targets") or not production.get("observed_at"):
        raise ValueError("existing production targets must be inspected first")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    started_at = now_iso()
    manifest = {
        "status": "blocked",
        "target_status": "not_generated",
        "synthetic": False,
        "adapter": "citic027_r150_t1",
        "as_of": as_of.isoformat(),
        "started_at": started_at,
        "available_at": None,
        "first_seen_at": None,
        "actual_target_calculations": 0,
        "memory_limit_bytes": limit,
        "output_directory": str(root),
        "read_only_database": True,
        "observer": {
            "host": socket.gethostname(),
            "checkout": str(ROOT),
            "git_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
        },
        "source_files": before,
        "source_tree_sha256": digest(before),
        "production_observation_sha256": file_sha(provenance_path),
        "production_checkout": production["checkout"],
        "production_git_head": production["git_head"],
        "lookback_days": LOOKBACK_DAYS,
        "valid_execution_interval": None,
        "daily_settlement_status": "not_adapted_not_an_m01_target_gate",
    }
    write_json(root / "production-observation.json", production)
    source = ReadOnlySource()
    phase = "read_only_connection"
    try:
        with source:
            phase = "exchange_frontier"
            frontier = source.frontier(as_of)
            cutoff = coverage_cutoff(
                dict(zip(frontier["exchange"], frontier["max_date"]))
            ).cutoff
            manifest["data_frontier"] = {
                r["exchange"]: r["max_date"].isoformat()
                for r in frontier.to_dict("records")
            }
            if cutoff != as_of:
                raise ValueError("current data frontier does not reach requested as_of")
            start = cutoff - timedelta(days=LOOKBACK_DAYS)
            config = replace(PRODUCTION, base_date=start)
            parameters = {k: scalar(v) for k, v in config.as_dict().items()}
            parameters.update(
                lookback_days=LOOKBACK_DAYS, signal_date=cutoff.isoformat()
            )
            write_json(root / "source-parameters.json", parameters)
            manifest["parameters_sha256"] = file_sha(root / "source-parameters.json")
            phase = "native_900_day_input_capture"
            prices = source.window(start, cutoff, root)
            if prices.empty or prices["trade_date"].max() != cutoff:
                raise ValueError("normalized input frontier mismatch")
            manifest["source_window"] = {
                "start": start.isoformat(),
                "end": cutoff.isoformat(),
                "normalized_rows": len(prices),
                "observed_at": now_iso(),
            }
            # Write the durable attempt marker BEFORE invoking the strategy once.
            manifest["actual_target_calculations"] = 1
            manifest["calculation_started_at"] = now_iso()
            write_json(root / "calculation-attempt.json", manifest)
            print(
                "M01: source reconciled; starting the single authorized target calculation",
                flush=True,
            )
            phase = "single_native_target_calculation"
            panel = build_panel(prices, config)
            phase = "native_factor_and_weights"
            result = build_from_panel(panel, config)
            del panel
            phase = "current_weight_projection"
            targets = current_weights(result, cutoff, config)
            manifest["calculation_completed_at"] = now_iso()
            del result
            targets.to_csv(root / "targets.csv", index=False)
            manifest["first_seen_at"] = now_iso()
            manifest["target_status"] = "generated_pending_release"
            manifest["row_count"] = len(targets)
            manifest["signal_date"] = cutoff.isoformat()
            manifest["gross_target_weight"] = float(
                targets["target_weight"].abs().sum()
            )
            phase = "current_contract_metadata"
            metadata = source.metadata(targets["contract"].tolist(), cutoff)
            metadata.to_csv(root / "source-contract-info.csv", index=False)
            evidence = preview_evidence(
                targets,
                prices,
                metadata,
                observed_at=manifest["source_window"]["observed_at"],
            )
            evidence["contract_metadata_observed_at"] = source.queries[-1][
                "observed_at"
            ]
            write_json(root / "preview-evidence.json", evidence)
        phase = "source_integrity_and_release"
        if source_files() != before:
            raise ValueError("source files changed during calculation")
        manifest["status"] = "target_ready_execution_blocked"
        manifest["target_status"] = "ready"
        manifest["available_at"] = (
            now_iso()
        )  # this new isolated publication, never a historical time
        manifest["source_version"] = (
            "citic027-r150-t1:" + manifest["source_tree_sha256"]
        )
        manifest["source_receipt"] = {
            "source_version": manifest["source_version"],
            "available_at": manifest["available_at"],
            "csv_sha256": file_sha(root / "targets.csv"),
            "metadata_sha256": file_sha(root / "source-parameters.json"),
            "row_count": len(targets),
            "signal_date": cutoff.isoformat(),
            "synthetic": False,
        }
    except Exception as exc:
        # No raw exception, SQL connection information or environment output.
        manifest["failure_type"] = type(exc).__name__
        manifest["failure_phase"] = phase
        manifest["failure_trace"] = safe_failure_trace(exc)
        manifest["status"] = "blocked"
    manifest["queries"] = source.queries
    manifest["completed_at"] = now_iso()
    manifest["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    manifest["files"] = {
        p.name: file_sha(p) for p in sorted(root.iterdir()) if p.is_file()
    }
    write_json(root / "manifest.pending.json", manifest)
    os.replace(root / "manifest.pending.json", root / "manifest.json")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--production-observation", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = run(args.as_of, args.production_observation, args.output_dir)
    except (ValueError, OSError, KeyError, TypeError):
        print(
            "M01: invalid preflight or destination; no target calculation started",
            file=sys.stderr,
        )
        return 2
    print(
        f"M01: {manifest['status']}; actual_target_calculations={manifest['actual_target_calculations']}"
    )
    return 0 if manifest["target_status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
