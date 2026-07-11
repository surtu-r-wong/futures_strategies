from contextlib import contextmanager
import psycopg2
from psycopg2 import sql


def pg_config_from(cfg: dict, use_test: bool = False) -> dict:
    key = "test_database" if use_test else "database"
    return cfg[key]


@contextmanager
def get_connection(pg_cfg: dict):
    conn = psycopg2.connect(
        host=pg_cfg["host"],
        port=pg_cfg["port"],
        dbname=pg_cfg["name"],
        user=pg_cfg["user"],
        password=pg_cfg["password"],
        # Long multi-step flows (e.g. universe_filter's 9 sequential queries)
        # leave the socket idle between Python-side steps; over Tailscale/NAT an
        # idle socket gets reaped mid-flow, surfacing as "server closed the
        # connection unexpectedly" on the next execute. TCP keepalive holds it.
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    try:
        schema = pg_cfg.get("schema")
        if schema:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            # Commit so search_path survives a later rollback() inside the
            # caller's transaction (SET inside a live txn is rolled back too).
            conn.commit()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
