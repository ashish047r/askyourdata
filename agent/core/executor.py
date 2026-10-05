"""Runs already-validated SQL as the read-only role, scoped to one client by Postgres row-level security."""

import datetime as dt
import os
import time
from decimal import Decimal

import psycopg
from psycopg import sql as pgsql

from .guards import MAX_ROWS


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time(0) else v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return v


def _connect(dsn: str, attempts: int = 3):
    """Network blips and Neon waking from scale-to-zero are transient: retry the CONNECT (never the query)."""
    for i in range(attempts):
        try:
            return psycopg.connect(dsn, connect_timeout=10)
        except psycopg.OperationalError:
            if i == attempts - 1:
                raise
            time.sleep(1 + i)


def run_sql(query: str, client_id: int, dsn: str | None = None) -> dict:
    """Returns {"columns", "rows", "truncated"}. Raises psycopg.Error on DB errors/timeouts."""
    with _connect(dsn or os.environ["DATABASE_URL_RO"]) as conn:
        with conn.cursor() as cur:
            # 1) tenant scope the RLS policies read (see data/migrations/0002); dropped at transaction end
            cur.execute(pgsql.SQL("CREATE TEMP TABLE tenant_scope ON COMMIT DROP AS SELECT {}::bigint AS id")
                        .format(pgsql.Literal(int(client_id))))
            # 2) from here on nothing can be written (the role also only has SELECT grants)
            cur.execute("SET LOCAL transaction_read_only = on")
            cur.execute("SET LOCAL statement_timeout = '5s'")
            # 3) exactly one LLM statement per transaction
            cur.execute(query)
            columns = [d.name for d in cur.description]
            rows = [[_jsonable(v) for v in r] for r in cur.fetchmany(MAX_ROWS)]
        conn.rollback()
    # ponytail: new connection per query (~100-300 ms to Neon); add a psycopg_pool if latency matters.
    return {"columns": columns, "rows": rows, "truncated": len(rows) >= MAX_ROWS}
