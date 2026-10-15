"""Local SQLite transactions, immutable ledger revisions and atomic feed export."""

from contextlib import contextmanager
import hashlib
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .contracts import (
    ENGINE_VERSION,
    SOURCE_RULES,
    canonical,
    digest,
    now_iso,
    text,
    timestamp,
    validate_config,
)
from .engine import calculate
from .fees import description as fee_description


def engine_fingerprint():
    root = Path(__file__).resolve().parents[1]
    paths = sorted(Path(__file__).parent.glob("*.py"))
    paths += [root / "common/minute/account.py", root / "common/errors.py"]
    return digest(
        {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths
        }
    )


@contextmanager
def connection(path, *, create=False):
    path = Path(path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise ValueError("account store does not exist; initialize explicitly")
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _schema(con):
    # Deliberately local SQLite only; these are never sent to Market Monitor.
    con.execute("""CREATE TABLE IF NOT EXISTS accounts (
        account_id TEXT PRIMARY KEY, config TEXT NOT NULL, calendar TEXT NOT NULL,
        initial_state TEXT NOT NULL, engine_hash TEXT NOT NULL, head INTEGER,
        identity TEXT NOT NULL UNIQUE)""")
    con.execute("""CREATE TABLE IF NOT EXISTS ledger (
        seq INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
        period_index INTEGER NOT NULL, revision INTEGER NOT NULL, base_seq INTEGER,
        input_hash TEXT NOT NULL, input_json TEXT NOT NULL, state_json TEXT NOT NULL,
        reason TEXT, UNIQUE(account_id, period_index, revision))""")
    con.execute("""CREATE TABLE IF NOT EXISTS attempts (
        account_id TEXT NOT NULL, checked_at TEXT NOT NULL, error TEXT NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS publications (
        content_hash TEXT PRIMARY KEY, payload TEXT NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS reference_updates (
        seq INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
        calendar TEXT NOT NULL, instruments TEXT NOT NULL, recorded_at TEXT NOT NULL)""")


def initialize(path, config, calendar):
    validate_config(config, calendar)
    initial_at = config["initial_at"] if config["synthetic"] else now_iso()
    # Skip the partly elapsed interval at bootstrap; stay flat until the next
    # full interval. No earlier days or partial-day zero returns are published.
    indices = [
        i
        for i, interval in enumerate(calendar["intervals"])
        if timestamp(interval["start_at"]) >= timestamp(initial_at)
    ]
    state = {
        "initialized_at": initial_at,
        "as_of": initial_at,
        "next_index": indices[0] if indices else None,
        "positions": {},
        "prices": {},
        "nav": config["initial_capital"],
        "cash": config["initial_capital"],
        "margin": 0.0,
        "free_cash": config["initial_capital"],
        "pnl": 0.0,
        "fees": 0.0,
        "row": None,
    }
    if "fee_model" in config:
        state["position_lots"] = {}
    with connection(path, create=True) as con:
        _schema(con)
        existing = con.execute(
            "SELECT * FROM accounts WHERE account_id=?", (config["account_id"],)
        ).fetchone()
        if existing:
            if existing["config"] != canonical(config) or existing[
                "calendar"
            ] != canonical(calendar):
                raise ValueError(
                    "account configuration/calendar is immutable; create a new version"
                )
            if existing["engine_hash"] != engine_fingerprint():
                raise ValueError("account engine release is immutable")
            return _state(con, existing)
        if not indices:
            raise ValueError(
                "calendar has no complete prospective interval after initialization"
            )
        identity = canonical(
            [SOURCE_RULES[config["adapter"]][0], config["account_version"]]
        )
        if con.execute(
            "SELECT 1 FROM accounts WHERE identity=?", (identity,)
        ).fetchone():
            raise ValueError(
                "duplicate strategy/account version; assign separate paper version"
            )
        con.execute(
            "INSERT INTO accounts VALUES (?,?,?,?,?,NULL,?)",
            (
                config["account_id"],
                canonical(config),
                canonical(calendar),
                canonical(state),
                engine_fingerprint(),
                identity,
            ),
        )
    return state


def _account(con, account_id):
    row = con.execute(
        "SELECT * FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone()
    if row is None:
        raise ValueError("unknown account_id")
    return row


def _state(con, account):
    if account["head"] is None:
        return json.loads(account["initial_state"])
    return json.loads(
        con.execute(
            "SELECT state_json FROM ledger WHERE seq=?", (account["head"],)
        ).fetchone()[0]
    )


def read_state(path, account_id):
    with connection(path) as con:
        return _state(con, _account(con, account_id))


def read_config(path, account_id):
    with connection(path) as con:
        return json.loads(_account(con, account_id)["config"])


def read_status(path, account_id):
    with connection(path) as con:
        account = _account(con, account_id)
        failure = con.execute(
            "SELECT * FROM attempts WHERE account_id IN (?, '*') ORDER BY rowid DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        _, calendar, reference = _references(con, account)
        state = _state(con, account)
        index = state["next_index"]
        return {
            "state": state,
            "reference_revision": reference,
            "next_interval": calendar["intervals"][index]
            if index < len(calendar["intervals"])
            else None,
            "last_failure": dict(failure) if failure else None,
            "engine_sha256": account["engine_hash"],
        }


def _references(con, account):
    config = json.loads(account["config"])
    latest = con.execute(
        "SELECT * FROM reference_updates WHERE account_id=? ORDER BY seq DESC LIMIT 1",
        (account["account_id"],),
    ).fetchone()
    if latest:
        config["instruments"] = json.loads(latest["instruments"])
        return config, json.loads(latest["calendar"]), latest["seq"]
    return config, json.loads(account["calendar"]), 0


def extend_references(path, account_id, calendar, instruments):
    """Append future sessions/contracts without resetting NAV or series identity.

    Existing session mappings and contract economics cannot be rewritten here.
    Validity may be extended only with the same specification/source. Changes
    to rates/multipliers require a separately designed effective-dated migration.
    """
    with connection(path) as con:
        account = _account(con, account_id)
        if account["engine_hash"] != engine_fingerprint():
            raise ValueError("engine release changed; explicit migration required")
        config, old_calendar, _ = _references(con, account)
        old_instruments = config["instruments"]
        count = len(old_calendar["intervals"])
        if calendar.get("intervals", [])[:count] != old_calendar["intervals"]:
            raise ValueError(
                "reference extension cannot rewrite existing calendar intervals"
            )
        for key in old_calendar.keys() - {"intervals", "coverage_end"}:
            if calendar.get(key) != old_calendar[key]:
                raise ValueError("calendar identity/source is immutable")
        for contract, old in old_instruments.items():
            new = instruments.get(contract)
            if (
                not new
                or any(new.get(k) != v for k, v in old.items() if k != "valid_to")
                or new["valid_to"] < old["valid_to"]
            ):
                raise ValueError("existing contract economics are immutable")
        config["instruments"] = instruments
        config["calendar_sha256"] = digest(calendar)
        validate_config(config, calendar)
        if calendar == old_calendar and instruments == old_instruments:
            return
        con.execute(
            "INSERT INTO reference_updates (account_id,calendar,instruments,recorded_at) VALUES (?,?,?,?)",
            (account_id, canonical(calendar), canonical(instruments), now_iso()),
        )


def advance(path, account_id, packet, *, revise_reason=None):
    try:
        return _advance(path, account_id, packet, revise_reason=revise_reason)
    except (ValueError, ArithmeticError) as exc:
        # Failure evidence is separate from the successful account snapshot.
        with connection(path) as con:
            con.execute(
                "INSERT INTO attempts VALUES (?,?,?)", (account_id, now_iso(), str(exc))
            )
        raise


def _advance(path, account_id, packet, *, revise_reason):
    input_hash = digest(packet)
    with connection(path) as con:
        account = _account(con, account_id)
        if account["engine_hash"] != engine_fingerprint():
            raise ValueError(
                "engine release changed; explicit migration/new account version required"
            )
        config, calendar, reference_revision = _references(con, account)
        intervals = calendar["intervals"]
        matches = [
            i
            for i, interval in enumerate(intervals)
            if interval["trade_date"] == packet.get("trade_date")
        ]
        if len(matches) != 1:
            raise ValueError("trade date outside explicit calendar coverage")
        index = matches[0]
        previous = con.execute(
            """SELECT * FROM ledger WHERE account_id=? AND period_index=?
            ORDER BY revision DESC LIMIT 1""",
            (account_id, index),
        ).fetchone()
        if previous and previous["input_hash"] == input_hash:
            return json.loads(previous["state_json"])
        base_seq = account["head"]
        revision = 1
        prior = _state(con, account)
        if previous:
            if previous["seq"] != account["head"]:
                raise ValueError(
                    "only tail revision supported; no implicit historical replay"
                )
            if not revise_reason:
                raise ValueError("changed input requires explicit revision reason")
            text(revise_reason, "revision reason")
            revision = previous["revision"] + 1
            base_seq = previous["base_seq"]
            prior = (
                json.loads(account["initial_state"])
                if base_seq is None
                else json.loads(
                    con.execute(
                        "SELECT state_json FROM ledger WHERE seq=?", (base_seq,)
                    ).fetchone()[0]
                )
            )
        elif revise_reason is not None:
            raise ValueError("revision requires an existing tail interval")
        if index != prior["next_index"]:
            raise ValueError(
                "next complete calendar interval required; no missing-day skip"
            )
        state = calculate(
            config, prior, intervals[index], packet, available_at=now_iso()
        )
        state["revision"] = revision
        state["audit"]["reference_revision"] = reference_revision
        seq = con.execute(
            """INSERT INTO ledger
            (account_id,period_index,revision,base_seq,input_hash,input_json,state_json,reason)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                account_id,
                index,
                revision,
                base_seq,
                input_hash,
                canonical(packet),
                canonical(state),
                revise_reason,
            ),
        ).lastrowid
        con.execute("UPDATE accounts SET head=? WHERE account_id=?", (seq, account_id))
        return state


def _atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (canonical(value) + "\n").encode()
    if path.exists() and path.read_bytes() == raw:
        return  # retain original mtime and generated_at on retries
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def export_feed(path, output, *, synthetic=False):
    # Serialize publishers across the outbox commit and filesystem replacement.
    # The immutable outbox preserves generated_at even after a process crash.
    if not Path(path).is_file():
        raise ValueError("account store does not exist")
    with Path(str(path) + ".publish.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _export_feed(path, output, synthetic=synthetic)
        except (ValueError, OSError) as exc:
            record_failure(path, "*", "export", exc)
            raise


def record_failure(path, account_id, operation, exc):
    with connection(path) as con:
        con.execute(
            "INSERT INTO attempts VALUES (?,?,?)",
            (account_id, now_iso(), f"{operation}: {type(exc).__name__}: {exc}"),
        )


def _export_feed(path, output, *, synthetic):
    output = Path(output).resolve()
    if synthetic and output.parts[-4:] == (
        "output",
        "multi_asset",
        "returns",
        "latest.json",
    ):
        raise ValueError("synthetic output must not use the official latest.json path")
    # Serialize export and ingestion with the same store transaction; an older
    # exporter cannot overwrite a newer committed feed in this store.
    with connection(path) as con:
        series = []
        for account in con.execute(
            "SELECT * FROM accounts ORDER BY account_id"
        ).fetchall():
            c = json.loads(account["config"])
            if c["synthetic"] is not synthetic:
                raise ValueError("synthetic/real stores must be exported separately")
            records = con.execute(
                """SELECT l.state_json FROM ledger l WHERE l.account_id=?
                AND l.revision=(SELECT MAX(x.revision) FROM ledger x
                    WHERE x.account_id=l.account_id AND x.period_index=l.period_index)
                ORDER BY l.period_index""",
                (account["account_id"],),
            ).fetchall()
            rows = [json.loads(r[0])["row"] for r in records]
            if not rows:
                continue
            series.append(
                {
                    "strategy": {
                        "source_project": "futures_strategies",
                        "strategy_id": SOURCE_RULES[c["adapter"]][0],
                        "version": f"{c['account_version']}:{digest(c)[:16]}:{account['engine_hash'][:12]}",
                    },
                    "kind": "strategy",
                    "mode": "paper",
                    "label": ("SYNTHETIC / " if synthetic else "") + c["label"],
                    "currency": c["currency"],
                    "return_basis": "net_nav_time_weighted",
                    "fee_model": f"{fee_description(c)}; {c['fee_description']}; "
                    f"{c['cash_rule']}; {c['financing_rule']}; {ENGINE_VERSION}",
                    "calendar_id": c["calendar_id"],
                    "inception_date": rows[0]["trade_date"],
                    "rows": rows,
                }
            )
        if not series:
            raise ValueError("no complete valuation interval; refusing empty feed")
        content_hash = digest(series)
        published = con.execute(
            "SELECT payload FROM publications WHERE content_hash=?", (content_hash,)
        ).fetchone()
        if published:
            feed = json.loads(published[0])
        else:
            feed = {
                "schema_version": 1,
                "source_project": "futures_strategies",
                "generated_at": now_iso(),
                "series": series,
            }
            con.execute(
                "INSERT INTO publications VALUES (?,?)", (content_hash, canonical(feed))
            )
    # Outbox is committed before any external file replacement. A failed
    # publish retries identical bytes; the accounting transaction is untouched.
    archive = output.parent / (output.stem + ".revisions") / (digest(feed) + ".json")
    _atomic_write(archive, feed)
    _atomic_write(output, feed)
    return feed
