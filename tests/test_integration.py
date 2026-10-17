"""End-to-end against the disposable stack (tests/stack/up first).

Proves, against real MySQL 8.0, PostgreSQL 16 and Redis 7.0 with the grants in
grants/ applied as written:
  - the MCP surface is exactly the catalog, and every tool works;
  - no tool changes database state;
  - no output carries a planted secret, password, row value or literal;
  - the observer credentials themselves cannot read rows, hashes or keys, or write;
  - diagnostics see real sessions and lock waits;
  - limits are enforced.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from dba_observer.config import catalog
from mcp_client import MCP

pytestmark = pytest.mark.integration

STACK = Path(__file__).parent / "stack"
URL = os.environ.get("DBA_OBSERVER_URL", "http://127.0.0.1:15000/mcp")

DEFAULT_ARGS = {
    "mysql_list_tables": {"database": "app_one"},
    "mysql_describe_table": {"database": "app_one", "table": "orders"},
    "mysql_list_indexes": {"database": "app_one"},
    "mysql_user_grants": {"user": "app_user", "host": "%"},
    "pg_list_schemas": {"database": "app_pg"},
    "pg_list_tables": {"database": "app_pg"},
    "pg_describe_table": {"database": "app_pg", "schema": "billing", "table": "invoices"},
    "pg_list_indexes": {"database": "app_pg"},
    "pg_role_privileges": {"role": "app_pg_user", "database": "app_pg"},
    "redis_acl_permissions": {"user": "app"},
    "mysql_table_fragmentation": {"min_free_bytes": 0},
    "pg_long_transactions": {"min_duration_secs": 0},
}


def dc(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", "-f", str(STACK / "compose.yaml"), *args],
                          input=input, capture_output=True, text=True, check=check)


def secret(file: str, key: str) -> str:
    for line in (STACK / ".secrets" / file).read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    raise KeyError(key)


def mysql_root(sql: str) -> str:
    return dc("exec", "-T", "mysql", "mysql", "-uroot", "-proot-test-only", "-N", "-e", sql).stdout.strip()


def pg_root(sql: str, db: str = "app_pg") -> str:
    return dc("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", db, "-At", "-c", sql).stdout.strip()


def redis_root(*cmd: str) -> str:
    return dc("exec", "-T", "redis", "redis-cli", "-a", "SEKRIT-REDIS-DEFAULT-PASS", "--no-auth-warning", *cmd).stdout.strip()


def snapshot() -> dict:
    return {
        "mysql_checksums": mysql_root("CHECKSUM TABLE app_one.customers, app_one.orders, app_one.audit_log, app_one.lockme"),
        "mysql_users": mysql_root("SELECT GROUP_CONCAT(CONCAT(User,'@',Host) ORDER BY User) FROM mysql.user"),
        "mysql_schemas": mysql_root("SELECT GROUP_CONCAT(SCHEMA_NAME ORDER BY 1) FROM information_schema.SCHEMATA"),
        "pg_rows": pg_root("SELECT md5(string_agg(t::text, ',' ORDER BY id)) FROM billing.invoices t"),
        "pg_lockme": pg_root("SELECT v FROM billing.lockme"),
        "pg_roles": pg_root("SELECT string_agg(rolname, ',' ORDER BY rolname) FROM pg_roles"),
        "pg_dbs": pg_root("SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database"),
        "pg_relations": pg_root("SELECT count(*) FROM pg_class"),
        "redis_dbsize": redis_root("DBSIZE"),
        "redis_value": redis_root("GET", "app:secret"),
        "redis_acl": redis_root("ACL", "LIST"),
        "redis_config_maxmemory": redis_root("CONFIG", "GET", "maxmemory"),
    }


@pytest.fixture(scope="module")
def mcp() -> MCP:
    try:
        httpx.get(URL.rsplit("/mcp", 1)[0] + "/", timeout=2)
    except httpx.HTTPError:
        pytest.skip("test stack not running: tests/stack/up")
    return MCP(URL)


@pytest.fixture(scope="module")
def live_sessions():
    """Long-running statements carrying a literal secret, plus one lock wait per engine."""
    procs = [
        # plain long statements with secret literals
        subprocess.Popen(["docker", "compose", "-f", str(STACK / "compose.yaml"), "exec", "-T", "mysql",
                          "mysql", "-uroot", "-proot-test-only", "-e", "SELECT SLEEP(25), 'SEKRIT-LITERAL-MYSQL'"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen(["docker", "compose", "-f", str(STACK / "compose.yaml"), "exec", "-T", "postgres",
                          "psql", "-U", "postgres", "-d", "app_pg", "-c", "SELECT pg_sleep(25), 'SEKRIT-LITERAL-PG'"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        # lock holders
        subprocess.Popen(["docker", "compose", "-f", str(STACK / "compose.yaml"), "exec", "-T", "mysql",
                          "mysql", "-uroot", "-proot-test-only", "-e",
                          "BEGIN; SELECT * FROM app_one.lockme WHERE id = 1 FOR UPDATE; DO SLEEP(25); ROLLBACK;"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen(["docker", "compose", "-f", str(STACK / "compose.yaml"), "exec", "-T", "postgres",
                          "psql", "-U", "postgres", "-d", "app_pg", "-c",
                          "BEGIN; SELECT * FROM billing.lockme WHERE id = 1 FOR UPDATE; SELECT pg_sleep(25); ROLLBACK;"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    time.sleep(2)
    procs += [
        # lock waiters (rolled back; they never commit a change)
        subprocess.Popen(["docker", "compose", "-f", str(STACK / "compose.yaml"), "exec", "-T", "mysql",
                          "mysql", "-uroot", "-proot-test-only", "-e",
                          "BEGIN; UPDATE app_one.lockme SET v = 42 WHERE id = 1; ROLLBACK;"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen(["docker", "compose", "-f", str(STACK / "compose.yaml"), "exec", "-T", "postgres",
                          "psql", "-U", "postgres", "-d", "app_pg", "-c",
                          "BEGIN; UPDATE billing.lockme SET v = 42 WHERE id = 1; ROLLBACK;"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    time.sleep(2)
    yield
    for p in procs:
        p.wait(timeout=60)


# --------------------------------------------------------------------------- surface

def test_surface_is_exactly_the_catalog(mcp):
    names = {t["name"] for t in mcp.list_tools()}
    assert names == {t["name"] for t in catalog()["tools"]}
    assert len(names) == 50


def test_every_tool_works_mutates_nothing_and_leaks_nothing(mcp, live_sessions):
    before = snapshot()
    outputs = {}
    for t in sorted(x["name"] for x in mcp.list_tools()):
        raw = mcp.raw_call(t, DEFAULT_ARGS.get(t, {}))
        res = json.loads(raw)
        assert not res.get("isError"), f"{t}: {raw[:400]}"
        err, payload = mcp.call(t, DEFAULT_ARGS.get(t, {}))
        if isinstance(payload, dict) and "status" in payload and "request_id" in payload:
            assert payload["status"] == "ok", f"{t}: {payload}"
        outputs[t] = raw
    after = snapshot()
    assert before == after, "a tool changed database state"

    blob = "\n".join(outputs.values())
    forbidden = ["SEKRIT", secret("toolbox.env", "MYSQL_PASSWORD"), secret("toolbox.env", "PG_PASSWORD"),
                 secret("adapter.env", "REDIS_PASSWORD"), "authentication_string", "SCRAM-SHA-256$"]
    for f in forbidden:
        assert f not in blob, f"{f!r} leaked: " + next(k for k, v in outputs.items() if f in v)


# --------------------------------------------------------------------------- semantics

def test_not_configured_replication_is_a_state_not_an_error(mcp):
    assert mcp.call("mysql_replication_status")[1]["replication_role"] == "not_configured"
    assert mcp.call("pg_replication_status")[1]["replication_role"] == "not_configured"
    assert mcp.call("redis_replication_status")[1]["data"]["replication"] == "not_configured"


def test_mysql_metadata_visible_rows_not(mcp):
    _, cols = mcp.call("mysql_describe_table", {"database": "app_one", "table": "orders"})
    assert [c["column_name"] for c in cols] == ["id", "customer_id", "total"]
    assert any("customers(id)" in (c["constraints"] or "") for c in cols)
    _, tables = mcp.call("mysql_list_tables", {"database": "app_one"})
    by_name = {t["table_name"]: t for t in tables}
    assert by_name["audit_log"]["has_unique_key"] == 0
    assert by_name["customers"]["has_primary_key"] == 1
    # app_hidden has no grant: invisible
    _, dbs = mcp.call("mysql_list_databases")
    assert "app_hidden" not in {d["database_name"] for d in dbs}


def test_user_grants(mcp):
    _, rows = mcp.call("mysql_user_grants", {"user": "app_user", "host": "%"})
    got = {(r["scope"], r["object"], r["privilege"]) for r in rows}
    assert ("database", "app_one.*", "UPDATE") in got
    assert ("table", "app_one.customers", "Select") in got
    assert not any(r["scope"] == "global" for r in rows)


def test_diagnostics_see_sessions_without_literals(mcp, live_sessions):
    _, my = mcp.call("mysql_active_sessions")
    my = my if isinstance(my, list) else [my]
    assert any("SLEEP" in (s.get("statement_digest") or "") for s in my), my
    _, pg = mcp.call("pg_active_sessions")
    pg = pg if isinstance(pg, list) else [pg]
    assert any("pg_sleep(?)" in (s.get("query_normalised") or "") for s in pg), pg
    assert "SEKRIT" not in json.dumps([my, pg])


def test_diagnostics_see_lock_waits(mcp, live_sessions):
    _, my = mcp.call("mysql_lock_waits")
    my = my if isinstance(my, list) else [my]
    assert any(w.get("locked_table") == "`app_one`.`lockme`" for w in my), my
    _, pg = mcp.call("pg_blocking_sessions")
    pg = pg if isinstance(pg, list) else [pg]
    assert pg and pg[0]["blocking_pid"], pg
    assert "42" not in json.dumps([w.get("waiting_query") for w in pg])


# --------------------------------------------------------------------------- limits and validation

def test_limits_enforced_by_toolbox(mcp):
    res = json.loads(mcp.raw_call("mysql_list_tables", {"database": "app_one", "limit": 501}))
    assert res.get("isError")
    res = json.loads(mcp.raw_call("mysql_slow_query_summary", {"order_by": "SUM_TIMER_WAIT; DROP"}))
    assert res.get("isError")


def test_adapter_rejects_bad_input(mcp):
    _, out = mcp.call("pg_list_tables", {"database": "postgres"})
    assert out["status"] == "not_configured"
    _, out = mcp.call("pg_describe_table", {"database": "app_pg", "schema": "billing", "table": "x' OR '1'='1"})
    assert out["status"] == "invalid_argument"
    _, out = mcp.call("redis_acl_permissions", {"user": "nobody"})
    assert out["status"] == "not_found"


# --------------------------------------------------------------------------- the credentials themselves

def test_mysql_observer_cannot_read_rows_hashes_or_write():
    pw = secret("toolbox.env", "MYSQL_PASSWORD")
    for sql, code in [("SELECT note FROM app_one.customers", "1142"),
                      ("SELECT authentication_string FROM mysql.user", "1143"),
                      ("CREATE TABLE app_one.x (i INT)", "1142"),
                      ("UPDATE app_one.lockme SET v = 1", "1142"),
                      ("SELECT * FROM app_hidden.t", "1142")]:
        r = dc("exec", "-T", "mysql", "mysql", "-h127.0.0.1", "-udba_observer", f"-p{pw}", "-e", sql, check=False)
        assert r.returncode != 0 and code in r.stderr, (sql, r.stderr)


def test_pg_observer_cannot_read_rows_or_write():
    pw = secret("adapter.env", "PG_PASSWORD")
    for sql in ("SELECT * FROM billing.invoices", "CREATE TABLE public.x (i int)", "UPDATE billing.lockme SET v = 1"):
        r = dc("exec", "-T", "-e", f"PGPASSWORD={pw}", "postgres", "psql", "-h", "127.0.0.1", "-U", "dba_observer",
               "-d", "app_pg", "-c", sql, check=False)
        assert r.returncode != 0 and ("permission denied" in r.stderr or "read-only" in r.stderr), (sql, r.stderr)


def test_redis_observer_cannot_read_keys_or_change_anything():
    pw = secret("adapter.env", "REDIS_PASSWORD")
    for cmd in (["GET", "app:secret"], ["KEYS", "*"], ["SCAN", "0"], ["CONFIG", "SET", "maxmemory", "1"],
                ["FLUSHALL"], ["MONITOR"], ["ACL", "SETUSER", "x"]):
        r = dc("exec", "-T", "redis", "redis-cli", "--user", "dba-observer", "--pass", pw, "--no-auth-warning", *cmd,
               check=False)
        assert "NOPERM" in r.stdout + r.stderr, (cmd, r.stdout, r.stderr)


def test_postgres_connections_use_tls_and_respect_caps(mcp):
    for _ in range(3):
        mcp.call("pg_connections")
        mcp.call("pg_list_tables", {"database": "app_pg"})
    rows = pg_root("SELECT a.application_name, s.ssl FROM pg_stat_activity a JOIN pg_stat_ssl s USING (pid) "
                   "WHERE a.usename = 'dba_observer'", db="postgres").splitlines()
    assert rows, "no observer connections seen"
    assert all(r.endswith("|t") for r in rows), rows
    toolbox_conns = [r for r in rows if r.startswith("dba-observer|")]
    assert len(toolbox_conns) <= 3, rows   # pool_max_conns
