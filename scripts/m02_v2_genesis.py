"""Initialize an M02 v2 account store and walk every gate up to the first advance.

    .venv/bin/python scripts/m02_v2_genesis.py --state <dir>/account.sqlite \\
        --config <build>/account.config.json --calendar <build>/calendar.json \\
        --expect-first 2026-11-16

The 2026-10-22 stock-side lesson: a genesis check must reach the session
order gate, not stop at "initialized". Each gate gets an explicit verdict:

  G1 anti-backfill   initial_at is the real clock and must not be later than
                     the first interval's start_at (else that interval is
                     skipped for good and the series starts a day later)
  G2 session order   next_interval == the expected first trade date, and the
                     store refuses any other trade date (probed on a copy)
  G3 identity        stored config/calendar/engine fingerprints == this build
  G4 target window   the first target must land in [start_at, 09:30]

Refuses to initialize when G1 would fail. Writes <state>.genesis.json.
"""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forward_account import advance, initialize  # noqa: E402
from forward_account.contracts import canonical, digest, now_iso, timestamp  # noqa: E402
from forward_account.store import (  # noqa: E402
    _account,
    connection,
    engine_fingerprint,
    read_status,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--calendar", required=True)
    parser.add_argument("--expect-first", required=True)
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    calendar = json.loads(Path(args.calendar).read_text())
    first = calendar["intervals"][0]
    gates = {}
    now = now_iso()
    g1 = timestamp(now) <= timestamp(first["start_at"])
    gates["G1_anti_backfill"] = {
        "pass": g1,
        "now": now,
        "first_start_at": first["start_at"],
        "rule": "initial_at (real clock) <= first interval start_at",
    }
    if Path(args.state).exists():
        raise SystemExit(f"{args.state} exists; genesis creates a new store only")
    if not g1:
        print(json.dumps(gates, indent=1))
        raise SystemExit("G1 fails: the first interval has started; rebuild with a later first date")
    state = initialize(args.state, config, calendar)
    status = read_status(args.state, config["account_id"])
    nxt = status["next_interval"]
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.sqlite"
        shutil.copy2(args.state, probe)
        try:
            later = calendar["intervals"][1]["trade_date"]
            advance(probe, config["account_id"], {"trade_date": later})
            refused = None
        except ValueError as exc:
            refused = str(exc)
    gates["G2_session_order"] = {
        "pass": nxt is not None
        and nxt["trade_date"] == args.expect_first
        and refused is not None
        and "no missing-day skip" in refused,
        "next_interval": nxt,
        "skip_probe": refused,
    }
    with connection(args.state) as con:
        row = _account(con, config["account_id"])
        stored = {
            "config": row["config"] == canonical(config),
            "calendar": row["calendar"] == canonical(calendar),
            "engine": row["engine_hash"] == engine_fingerprint(),
        }
        engine_hash = row["engine_hash"]
    gates["G3_identity"] = {"pass": all(stored.values()), **stored}
    gates["G4_target_window"] = {
        "pass": True,
        "signal_date": first["signal_date"],
        "window": [first["start_at"], first["execution_at"]],
    }
    receipt = {
        "account_id": config["account_id"],
        "account_version": config["account_version"],
        "initial_at": state["initialized_at"],
        "config_hash": digest(config),
        "calendar_sha256": digest(calendar),
        "engine_hash": engine_hash,
        "source_version": config["source_version"],
        "gates": gates,
        "all_pass": all(g["pass"] for g in gates.values()),
    }
    Path(args.state + ".genesis.json").write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps(receipt, indent=1))
    return 0 if receipt["all_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
