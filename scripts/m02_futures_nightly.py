"""M02 futures nightly advance: coverage gate, catch-up loop, verdict JSON.

Unattended Debian crontab entry (2026-10-27 dispatch; runs 21:05 on
trading evenings after public.futures_daily has the day's settles):

- SKIP when no interval is due (weekend/holiday/already advanced);
- FAIL closed, zero write, when the next interval's settle coverage is
  incomplete for the held contracts (missing row / NULL / nonpositive);
- advance every due interval in calendar order through the same
  m02_futures_advance CLI the session chain used (dry run, then commit
  + feed export), so a missed night is caught up in one loop;
- self-check the money chain per interval (daily_return x previous NAV
  must equal the day's PnL) and stamp a PASS/FAIL/SKIP verdict JSON
  with the engine fingerprint and the key row for next-day audit.

A failing step writes nothing for that step; intervals committed earlier
in the same catch-up loop stay committed (each is a fully gated, settled
day) and are listed in the verdict. Exit codes: 0 PASS/SKIP, 2 FAIL.
"""

import argparse
import datetime as dt
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import m02_futures_advance as adv  # noqa: E402
from forward_account.store import read_status  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ACCOUNT_DEFAULT = "m02-futures-observation"
STATE_DEFAULT = adv.STATE_DEFAULT
FEED_DEFAULT = adv.FEED_DEFAULT
CONFIG_DEFAULT = adv.BUILD_DIR / "account.config.json"
MAX_INTERVALS_DEFAULT = 15
TZ = dt.timezone(dt.timedelta(hours=8))
VERDICT_SCHEMA = "m02-futures-nightly-verdict.v1"


def settle_gate(prices):
    """Problems for held contracts whose settle is NULL or nonpositive."""
    bad = sorted(c for c, p in prices.items() if p is None or p <= 0)
    return [f"nonpositive settle: {', '.join(bad)}"] if bad else []


def money_chain(prev_nav, row, nav):
    """daily_return x previous NAV must reconstruct the day's PnL."""
    residual = row["daily_return"] * prev_nav - (nav - prev_nav)
    ok = abs(residual) <= max(1e-6, abs(nav) * 1e-12)
    return ok, residual


def settles(trade_date, wanted):
    """{contract: settle-or-None} for the held contracts on trade_date.

    Mirrors the exact query m02_futures_advance._settlement runs one step
    later, but tolerates NULL settles so the gate can name the contract
    instead of crashing on float(None).
    """
    with adv.get_connection(
        adv.pg_config_from(adv.load_config(adv.resolve_settings_path()))
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, settle FROM public.futures_daily "
                "WHERE trade_date = %s AND symbol = ANY(%s)",
                (trade_date, sorted(wanted)),
            )
            return {
                r[0]: (None if r[1] is None else float(r[1])) for r in cur.fetchall()
            }


def _git_commit():
    try:
        return subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _finish(verdict, verdict_dir, today):
    verdict["finished_at"] = dt.datetime.now(TZ).isoformat()
    payload = json.dumps(verdict, ensure_ascii=False, indent=1) + "\n"
    _atomic_write(Path(verdict_dir) / f"{today}.json", payload)
    _atomic_write(Path(verdict_dir) / "latest.json", payload)
    print(json.dumps(verdict, ensure_ascii=False))
    return 0 if verdict["status"] in ("PASS", "SKIP") else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(STATE_DEFAULT))
    parser.add_argument("--account", default=ACCOUNT_DEFAULT)
    parser.add_argument("--config", default=str(CONFIG_DEFAULT))
    parser.add_argument("--feed", default=str(FEED_DEFAULT))
    parser.add_argument("--verdict-dir", default=None)
    parser.add_argument(
        "--today",
        default=None,
        help="override the trading date gate (testing; default: Beijing today)",
    )
    parser.add_argument("--max-intervals", type=int, default=MAX_INTERVALS_DEFAULT)
    args = parser.parse_args(argv)

    today = args.today or dt.datetime.now(TZ).date().isoformat()
    verdict_dir = args.verdict_dir or str(Path(args.state).parent / "verdicts")
    verdict = {
        "schema": VERDICT_SCHEMA,
        "started_at": dt.datetime.now(TZ).isoformat(),
        "host": socket.gethostname(),
        "commit": _git_commit(),
        "today": today,
    }
    rows = []
    try:
        status = read_status(args.state, args.account)
        verdict["engine_sha256"] = status["engine_sha256"]
        verdict["store_head_at_start"] = (
            status["state"]["row"]["trade_date"] if status["state"]["row"] else None
        )
        nxt = status["next_interval"]
        if nxt is None:
            verdict.update(
                status="FAIL",
                stage="calendar",
                error="calendar exhausted; extend references first",
                rows=rows,
            )
            return _finish(verdict, verdict_dir, today)
        if nxt["trade_date"] > today:
            verdict.update(status="SKIP", next_interval=nxt, note="nothing due tonight")
            return _finish(verdict, verdict_dir, today)

        common = [
            "--state",
            args.state,
            "--account",
            args.account,
            "--config",
            args.config,
            "--feed",
            args.feed,
        ]
        while True:
            positions = status["state"]["positions"]
            if not positions:
                verdict.update(
                    status="FAIL",
                    stage="positions",
                    error="no open positions: unattended build day is not "
                    "supported (needs the Wind CZCE receipt)",
                    rows=rows,
                )
                return _finish(verdict, verdict_dir, today)
            if len(rows) >= args.max_intervals:
                verdict.update(
                    status="FAIL",
                    stage="cap",
                    error=f"interval cap {args.max_intervals} reached with "
                    "more dates still due; investigate before widening",
                    rows=rows,
                )
                return _finish(verdict, verdict_dir, today)

            trade_date = nxt["trade_date"]
            prev_nav = status["state"]["nav"]
            prices = settles(trade_date, set(positions))
            missing = sorted(set(positions) - prices.keys())
            problems = (
                [f"missing settle row: {', '.join(missing)}"] if missing else []
            ) + settle_gate(prices)
            if problems:
                verdict.update(
                    status="FAIL",
                    stage="settle_gate",
                    trade_date=trade_date,
                    failed_at=trade_date,
                    error="; ".join(problems),
                    rows=rows,
                )
                return _finish(verdict, verdict_dir, today)

            adv.main(common + ["--dry-run"])  # packet + pinned-target re-check
            adv.main(common)  # commit + feed export (single-command form)
            status = read_status(args.state, args.account)
            state = status["state"]
            row = state["row"]
            ok, residual = money_chain(prev_nav, row, state["nav"])
            rows.append(
                {
                    "trade_date": row["trade_date"],
                    "unit_nav": row["unit_nav"],
                    "daily_return": row["daily_return"],
                    "nav": state["nav"],
                    "margin": state["margin"],
                    "margin_ratio": row["margin_ratio"],
                    "gross_ratio": row["gross_ratio"],
                    "positions": len(state["positions"]),
                    "fees": state["fees"],
                    "pnl_residual": residual,
                }
            )
            verdict["rows"] = rows
            if not ok:
                verdict.update(
                    status="FAIL",
                    stage="money_chain",
                    trade_date=row["trade_date"],
                    failed_at=row["trade_date"],
                    error=f"daily_return x prev_nav != nav - prev_nav "
                    f"(residual {residual:.6f} CNY)",
                )
                return _finish(verdict, verdict_dir, today)
            nxt = status["next_interval"]
            if nxt is None:
                verdict["calendar_exhausted_after_run"] = True
                break
            if nxt["trade_date"] > today:
                break

        verdict["status"] = "PASS"
        return _finish(verdict, verdict_dir, today)
    except Exception as exc:  # verdict always lands, even on surprise paths
        verdict.update(
            status="FAIL",
            stage="exception",
            error=f"{type(exc).__name__}: {exc}",
            rows=rows,
        )
        return _finish(verdict, verdict_dir, today)


if __name__ == "__main__":
    raise SystemExit(main())
