"""Phase 0 tools: targets, capabilities, health, policy, audit status."""

from __future__ import annotations

import socket
import time

from . import audit, pg_catalog, redis_tools
from .config import VERSION, Settings, catalog
from .errors import classify


def _target_address(s: Settings, engine: str) -> tuple[str | None, int]:
    return {
        "mysql": (s.mysql_host, s.mysql_port),
        "postgresql": (s.pg_host, s.pg_port),
        "redis": (s.redis_host, s.redis_port),
    }[engine]


def list_targets(s: Settings, _: dict) -> dict:
    rows = []
    for t in catalog()["targets"]:
        host, port = _target_address(s, t["engine"])
        row = {"target": t["id"], "engine": t["engine"], "configured": bool(host), "host": host, "port": port}
        if t["engine"] == "postgresql":
            row["catalog_databases"] = s.pg_databases
        rows.append(row)
    return {"targets": rows}


def get_capabilities(s: Settings, _: dict) -> dict:
    c = catalog()
    configured = {t["target"]: t["configured"] for t in list_targets(s, {})["targets"]}
    tools = []
    for t in c["tools"]:
        engine = t["engine"]
        target = {"mysql": "mysql", "postgresql": "postgresql", "redis": "redis"}.get(engine)
        tools.append({
            **t,
            "tier": c["tier"],
            "available": configured.get(target, True) if target else True,
        })
    return {
        "server": c["server"], "version": VERSION, "tier": c["tier"],
        "engines": ["mysql", "postgresql", "redis"],
        "phases_included": sorted({t["phase"] for t in c["tools"]}),
        "tool_count": len(tools), "tools": tools,
    }


def _probe(fn) -> dict:
    t0 = time.perf_counter()
    try:
        detail = fn()
        return {"status": "ok", "latency_ms": round((time.perf_counter() - t0) * 1000, 1), **(detail or {})}
    except Exception as exc:  # noqa: BLE001 - every failure becomes a status
        e = classify(exc)
        return {"status": e.status, "code": e.code, "message": e.message}


def _tcp(host: str, port: int) -> dict:
    with socket.create_connection((host, port), timeout=2):
        return {"check": "tcp_connect"}


def tool_health(s: Settings, _: dict) -> dict:
    components = {"adapter": {"status": "ok", "version": VERSION}, "audit": audit.status()}
    components["audit"]["status"] = "ok" if components["audit"]["last_write_ok"] is not False else "error"

    dbs: dict[str, dict] = {}
    if s.redis_configured:
        dbs["redis"] = _probe(lambda: (redis_tools.client(s).ping(), {"check": "PING with observer credential"})[1])
    else:
        dbs["redis"] = {"status": "not_configured"}

    if s.pg_configured:
        def pg():
            with pg_catalog.connect(s) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
            return {"check": "SELECT 1 with observer credential on the home database"}
        dbs["postgresql"] = _probe(pg)
    else:
        dbs["postgresql"] = {"status": "not_configured"}

    # The adapter holds no MySQL credential (Toolbox does); mysql_health checks the credential.
    if s.mysql_host:
        dbs["mysql"] = _probe(lambda: _tcp(s.mysql_host, s.mysql_port))
    else:
        dbs["mysql"] = {"status": "not_configured"}

    components["toolbox"] = {"status": "ok", "check": "this call was routed through it"}
    overall = "ok" if all(d["status"] in ("ok", "not_configured") for d in dbs.values()) else "degraded"
    return {"overall": overall, "components": components, "databases": dbs}


def get_policy(s: Settings, _: dict) -> dict:
    c = catalog()
    return {
        "tier": c["tier"],
        "limits": c["limits"],
        "guarantees": c["guarantees"],
        "approval": "Not applicable: this server has no mutating tools. Administration is a separate server (dba-admin) gated by operator approval.",
        "postgresql_catalog_databases": s.pg_databases,
        "access": "Cloudflare Access in front of the public route; LAN callers are not authenticated (owner decision, read-only tier).",
    }


def get_audit_status(s: Settings, _: dict) -> dict:
    return {
        "adapter": audit.status(),
        "toolbox": {
            "sink": "container stdout, JSON (--logging-format JSON --log-level DEBUG)",
            "covers": "every MCP request; for each tools/call the tool name and bound parameters, correlated by trace id. No SQL text, rows or credentials.",
        },
        "caller_identity": "Recorded by Cloudflare Access at the edge; not visible to this server.",
        "portal_tool_call_logs": "unknown (not verified)",
        "failure_policy": "Audit writes go to stdout. If a write fails the call still completes and last_write_ok turns false; tool_health then reports audit as error.",
    }
