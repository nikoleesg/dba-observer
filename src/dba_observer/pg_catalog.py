"""PostgreSQL per-database catalog tools.

Toolbox binds a PostgreSQL source to one database, but schemas, tables, columns
and object grants are only visible from inside the database that holds them. So
these five tools live here and connect to the requested database, which must be
on the PG_DATABASES allowlist. Cluster-wide tools (activity, replication, roles,
settings) stay in Toolbox on the home database.

Everything reads pg_catalog only. Nothing selects from a user table.
"""

from __future__ import annotations

import re
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from .config import Settings
from .errors import ToolError, invalid, not_configured

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")

# %% because every statement here is executed with parameters (psycopg placeholders).
_SYSTEM_SCHEMA_FILTER = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_toast%%' AND n.nspname NOT LIKE 'pg\\_temp\\_%%'"
)


def ident(value: str, what: str) -> str:
    if not IDENT_RE.fullmatch(value):
        raise invalid(f"{what} must be a plain identifier (letters, digits, _ $ -; max 63)")
    return value


@contextmanager
def connect(s: Settings, database: str | None = None):
    if not s.pg_configured:
        raise not_configured("PostgreSQL")
    db = database or s.pg_home_database
    if database is not None and database not in s.pg_databases:
        raise ToolError(
            "not_configured", "database_not_allowed",
            f"Database is not on this server's allowlist. Allowed: {', '.join(s.pg_databases) or '(none)'}",
        )
    conn = psycopg.connect(
        host=s.pg_host, port=s.pg_port, dbname=db, user=s.pg_user, password=s.pg_password,
        sslmode=s.pg_sslmode, connect_timeout=3, application_name="dba-observer-adapter",
        options="-c statement_timeout=5000 -c lock_timeout=1000 -c default_transaction_read_only=on",
        row_factory=dict_row,
    )
    try:
        conn.read_only = True
        yield conn
    finally:
        conn.close()


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _limited(rows: list[dict], limit: int) -> dict:
    return {"rows": rows[:limit], "truncated": len(rows) > limit}


def list_schemas(s: Settings, a: dict) -> dict:
    db = ident(a["database"], "database")
    sql = f"""
        SELECT n.nspname AS schema, pg_get_userbyid(n.nspowner) AS owner,
               (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind IN ('r','p')) AS tables,
               (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind IN ('v','m')) AS views,
               obj_description(n.oid, 'pg_namespace') AS comment
        FROM pg_namespace n
        WHERE {_SYSTEM_SCHEMA_FILTER}
        ORDER BY n.nspname
        LIMIT %(lim)s"""
    with connect(s, db) as conn:
        return {"database": db, **_limited(_rows(conn, sql, {"lim": a["limit"] + 1}), a["limit"])}


def list_tables(s: Settings, a: dict) -> dict:
    db = ident(a["database"], "database")
    schema = ident(a["schema"], "schema") if a.get("schema") else ""
    sql = f"""
        SELECT n.nspname AS schema, c.relname AS name,
               CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned_table' WHEN 'v' THEN 'view'
                              WHEN 'm' THEN 'materialized_view' WHEN 'f' THEN 'foreign_table' END AS kind,
               pg_get_userbyid(c.relowner) AS owner, c.relispartition AS is_partition,
               CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS approx_rows,
               pg_total_relation_size(c.oid) AS total_bytes,
               EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = c.oid AND k.contype = 'p') AS has_primary_key
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','f') AND {_SYSTEM_SCHEMA_FILTER}
          AND (%(schema)s = '' OR n.nspname = %(schema)s)
        ORDER BY total_bytes DESC NULLS LAST, n.nspname, c.relname
        LIMIT %(lim)s"""
    with connect(s, db) as conn:
        rows = _rows(conn, sql, {"schema": schema, "lim": a["limit"] + 1})
    return {"database": db, **_limited(rows, a["limit"])}


def describe_table(s: Settings, a: dict) -> dict:
    db = ident(a["database"], "database")
    p = {"schema": ident(a["schema"], "schema"), "table": ident(a["table"], "table")}
    rel_sql = """
        SELECT c.oid, CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned_table' WHEN 'v' THEN 'view'
                      WHEN 'm' THEN 'materialized_view' WHEN 'f' THEN 'foreign_table' ELSE c.relkind::text END AS kind,
               pg_get_userbyid(c.relowner) AS owner, obj_description(c.oid, 'pg_class') AS comment
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %(schema)s AND c.relname = %(table)s"""
    col_sql = """
        SELECT a.attnum AS position, a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
               NOT a.attnotnull AS nullable, pg_get_expr(d.adbin, d.adrelid) AS default,
               a.attidentity <> '' AS is_identity, a.attgenerated <> '' AS is_generated,
               col_description(a.attrelid, a.attnum) AS comment
        FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum"""
    con_sql = """
        SELECT k.conname AS name,
               CASE k.contype WHEN 'p' THEN 'primary_key' WHEN 'u' THEN 'unique' WHEN 'f' THEN 'foreign_key'
                              WHEN 'c' THEN 'check' WHEN 'x' THEN 'exclusion' ELSE k.contype::text END AS type,
               pg_get_constraintdef(k.oid) AS definition
        FROM pg_constraint k WHERE k.conrelid = %(oid)s ORDER BY k.contype, k.conname"""
    with connect(s, db) as conn:
        rel = _rows(conn, rel_sql, p)
        if not rel:
            raise ToolError("not_found", "relation_not_found", "No table or view with that schema and name")
        oid = rel[0].pop("oid")
        cols = _rows(conn, col_sql, {"oid": oid})
        cons = _rows(conn, con_sql, {"oid": oid})
    return {"database": db, **p, **rel[0], "columns": cols, "constraints": cons}


def list_indexes(s: Settings, a: dict) -> dict:
    db = ident(a["database"], "database")
    schema = ident(a["schema"], "schema") if a.get("schema") else ""
    table = ident(a["table"], "table") if a.get("table") else ""
    sql = f"""
        SELECT n.nspname AS schema, t.relname AS table, i.relname AS index,
               ix.indisunique AS is_unique, ix.indisprimary AS is_primary, ix.indisvalid AS is_valid,
               pg_get_indexdef(ix.indexrelid) AS definition, pg_relation_size(ix.indexrelid) AS bytes,
               s.idx_scan AS scans
        FROM pg_index ix
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_class t ON t.oid = ix.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        LEFT JOIN pg_stat_all_indexes s ON s.indexrelid = ix.indexrelid
        WHERE {_SYSTEM_SCHEMA_FILTER}
          AND (%(schema)s = '' OR n.nspname = %(schema)s)
          AND (%(table)s = '' OR t.relname = %(table)s)
        ORDER BY bytes DESC, n.nspname, t.relname, i.relname
        LIMIT %(lim)s"""
    with connect(s, db) as conn:
        rows = _rows(conn, sql, {"schema": schema, "table": table, "lim": a["limit"] + 1})
    return {"database": db, **_limited(rows, a["limit"])}


def role_privileges(s: Settings, a: dict) -> dict:
    role = ident(a["role"], "role")
    database = ident(a["database"], "database") if a.get("database") else None
    role_sql = """
        SELECT r.rolname AS role, r.rolsuper AS superuser, r.rolcanlogin AS can_login,
               r.rolcreatedb AS create_db, r.rolcreaterole AS create_role, r.rolreplication AS replication,
               r.rolbypassrls AS bypass_rls, r.rolinherit AS inherit, r.rolconnlimit AS connection_limit,
               r.rolvaliduntil AS valid_until,
               ARRAY(SELECT b.rolname FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.roleid
                     WHERE m.member = r.oid ORDER BY 1) AS member_of,
               ARRAY(SELECT b.rolname FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.member
                     WHERE m.roleid = r.oid ORDER BY 1) AS members
        FROM pg_roles r WHERE r.rolname = %(role)s"""
    db_sql = """
        SELECT d.datname AS database, pg_get_userbyid(d.datdba) = %(role)s AS owner,
               has_database_privilege(%(role)s, d.oid, 'CONNECT') AS connect,
               has_database_privilege(%(role)s, d.oid, 'CREATE') AS "create",
               has_database_privilege(%(role)s, d.oid, 'TEMP') AS temp
        FROM pg_database d WHERE d.datallowconn ORDER BY d.datname"""
    schema_sql = f"""
        SELECT n.nspname AS schema, pg_get_userbyid(n.nspowner) = %(role)s AS owner,
               has_schema_privilege(%(role)s, n.oid, 'USAGE') AS usage,
               has_schema_privilege(%(role)s, n.oid, 'CREATE') AS "create"
        FROM pg_namespace n WHERE {_SYSTEM_SCHEMA_FILTER} ORDER BY n.nspname"""
    table_sql = f"""
        SELECT n.nspname AS schema, count(*) AS relations,
               count(*) FILTER (WHERE has_table_privilege(%(role)s, c.oid, 'SELECT')) AS "select",
               count(*) FILTER (WHERE has_table_privilege(%(role)s, c.oid, 'INSERT')) AS "insert",
               count(*) FILTER (WHERE has_table_privilege(%(role)s, c.oid, 'UPDATE')) AS "update",
               count(*) FILTER (WHERE has_table_privilege(%(role)s, c.oid, 'DELETE')) AS "delete",
               count(*) FILTER (WHERE has_table_privilege(%(role)s, c.oid, 'TRUNCATE')) AS "truncate",
               count(*) FILTER (WHERE pg_get_userbyid(c.relowner) = %(role)s) AS owned
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','f') AND {_SYSTEM_SCHEMA_FILTER}
        GROUP BY n.nspname ORDER BY n.nspname"""
    p = {"role": role}
    with connect(s, database) as conn:
        r = _rows(conn, role_sql, p)
        if not r:
            raise ToolError("not_found", "role_not_found", "No role with that name")
        out = {**r[0], "databases": _rows(conn, db_sql, p)}
        if database:
            out["database"] = database
            out["schemas"] = _rows(conn, schema_sql, p)
            out["relations_by_schema"] = _rows(conn, table_sql, p)
    return out
