"""Offline checks of the startup timezone sent to each active PG connection."""

from types import SimpleNamespace

import pytest

from citic_index import pg_source
from common import db
from forward_inputs import m01
from index_open_momentum import __main__ as index_cli
from scripts import citic_index_fetch, citic_receipts_exchange_fetch
from scripts.research import audit_fundamentals


CONFIG = dict(host="invalid.test", port=5432, name="test", user="test", password="test")


@pytest.fixture
def connections(monkeypatch):
    opened = []

    class Connection:
        description = [("test",)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self, **kwargs):
            return self

        def execute(self, *args):
            pass

        def fetchone(self):
            return ("on",)

        def fetchall(self):
            return []

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

        def set_session(self, **kwargs):
            pass

    def connect(**kwargs):
        opened.append(kwargs)
        return Connection()

    monkeypatch.setattr(db.psycopg2, "connect", connect)
    return opened


def assert_market_timezone(opened):
    assert opened
    assert all(conn.get("options") == "-c timezone=Asia/Shanghai" for conn in opened)


def test_common_connection_passes_market_timezone_on_each_connection(connections):
    with db.get_connection(CONFIG) as connection:
        connection.rollback()
    with db.get_connection(CONFIG):
        pass
    assert len(connections) == 2
    assert_market_timezone(connections)


@pytest.mark.parametrize("module", [pg_source, citic_index_fetch, citic_receipts_exchange_fetch])
def test_fetch_connections_pass_market_timezone(module, monkeypatch, connections):
    monkeypatch.setattr(module, "load_config", lambda _: {"database": CONFIG})
    monkeypatch.setattr(module, "resolve_settings_path", lambda: "/fake/settings")
    dsn = module._dsn()
    module._query(dsn, "SELECT 1", {}, attempts=1, timeout="1s")
    module._query(dsn, "SELECT 1", {}, attempts=1, timeout="1s")
    assert len(connections) == 2
    assert_market_timezone(connections)


def test_forward_snapshot_inherits_market_timezone(monkeypatch, connections):
    monkeypatch.setattr(pg_source, "load_config", lambda _: {"database": CONFIG})
    monkeypatch.setattr(pg_source, "resolve_settings_path", lambda: "/fake/settings")
    with m01.ReadOnlySource():
        pass
    assert_market_timezone(connections)


def test_index_cli_connection_has_market_timezone(connections):
    index_cli._fetch("SELECT 1", (), CONFIG)
    assert_market_timezone(connections)


def test_fundamental_audit_connection_has_market_timezone(monkeypatch, tmp_path, connections):
    monkeypatch.setattr(audit_fundamentals, "load_config", lambda _: {"database": CONFIG})
    monkeypatch.setattr(audit_fundamentals, "resolve_settings_path", lambda: "/fake/settings")
    monkeypatch.setattr(audit_fundamentals.argparse.ArgumentParser, "parse_args",
                        lambda _: SimpleNamespace(output=tmp_path / "audit.json"))
    monkeypatch.setattr(audit_fundamentals, "collect", lambda _: {
        "build": {"build_version": "test"},
        "checks": {"output_rows_match": True, "integrity_zero": True,
                   "catalog_hash_match": True, "direct_staleness_pass": True,
                   "source_shape_pass": True, "inventory_nonnegative": True,
                   "unwaived_daily_floor_failures": []},
    })
    assert audit_fundamentals.main() == 0
    assert_market_timezone(connections)
