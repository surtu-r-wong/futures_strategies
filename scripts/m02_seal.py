"""Seal an M02 futures account store as a closed historical series.

    .venv/bin/python scripts/m02_seal.py --state <dir>/account.sqlite \\
        --account m02-futures-observation --feed <dir>/feed/latest.json \\
        --expect-last 2026-11-13 --reason "v1 closed per 2026-10-28 ruling"

Read-only on the store contents: the store and feed bytes stay exactly as
they are (the seal is a sidecar, not a table). It verifies the last
published interval is the expected one, records the store/feed SHA-256, and
removes write permission from the store, its WAL/journal siblings and the
feed so that no later advance can write (SQLite refuses a read-only file).
The v2 nightly treats the sidecar as SEALED and does nothing.
"""

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forward_account.contracts import now_iso  # noqa: E402
from forward_account.store import read_status  # noqa: E402


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_only(path):
    mode = os.stat(path).st_mode
    os.chmod(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def seal(state, account, feed, expect_last, reason):
    sidecar = Path(str(state) + ".sealed.json")
    if sidecar.exists():
        raise ValueError(f"{state} is already sealed")
    status = read_status(state, account)
    row = status["state"]["row"]
    if row is None or row["trade_date"] != expect_last:
        raise ValueError(
            f"last interval is {row and row['trade_date']}, expected {expect_last}; not sealing"
        )
    record = {
        "schema": "m02-sealed-store.v1",
        "account_id": account,
        "sealed_at": now_iso(),
        "reason": reason,
        "last_trade_date": row["trade_date"],
        "last_row": row,
        "nav": status["state"]["nav"],
        "engine_sha256": status["engine_sha256"],
        "store_sha256": sha(state),
        "feed": str(feed) if feed else None,
        "feed_sha256": sha(feed) if feed else None,
    }
    sidecar.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n")
    for path in [state, *(Path(str(state) + s) for s in ("-wal", "-journal", "-shm"))]:
        if Path(path).exists():
            read_only(path)
    if feed:
        read_only(feed)
    read_only(sidecar)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--feed")
    parser.add_argument("--expect-last", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    record = seal(args.state, args.account, args.feed, args.expect_last, args.reason)
    print(json.dumps(record, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
