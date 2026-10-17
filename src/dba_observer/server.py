"""The adapter's HTTP surface.

Only Toolbox calls this, over an internal network. Every route is a GET with
query parameters that are validated here even though Toolbox validated them
first. Every response is HTTP 200 with a status envelope, so Toolbox passes the
JSON through and the agent sees an actionable status rather than a bare
"unexpected status code".

    {"status": "ok" | "unavailable" | "not_configured" | "unsupported" | "not_found"
               | "permission_denied" | "invalid_argument" | "busy" | "error",
     "tool": ..., "engine": ..., "request_id": ..., "data": {...} | "error": {"code", "message"}}
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import audit, meta, pg_catalog, redis_tools
from .config import VERSION, Settings, catalog
from .errors import ToolError, classify, invalid

log = logging.getLogger("dba_observer")


@dataclass(frozen=True)
class P:
    name: str
    kind: str = "str"            # str | int
    required: bool = False
    default: Any = None
    min: int | None = None
    max: int | None = None


def _limit(default: int = 25, maximum: int = 500) -> P:
    return P("limit", "int", default=default, min=1, max=maximum)


@dataclass(frozen=True)
class Route:
    tool: str
    engine: str
    fn: Callable[[Settings, dict], dict]
    params: tuple[P, ...] = ()


ROUTES: dict[str, Route] = {r.tool: r for r in (
    # phase 0
    Route("list_targets", "any", meta.list_targets),
    Route("get_capabilities", "any", meta.get_capabilities),
    Route("tool_health", "any", meta.tool_health),
    Route("get_policy", "any", meta.get_policy),
    Route("get_audit_status", "any", meta.get_audit_status),
    # redis
    Route("redis_health", "redis", redis_tools.health),
    Route("redis_memory", "redis", redis_tools.memory),
    Route("redis_clients", "redis", redis_tools.clients),
    Route("redis_keyspace_stats", "redis", redis_tools.keyspace_stats),
    Route("redis_persistence", "redis", redis_tools.persistence),
    Route("redis_replication_status", "redis", redis_tools.replication),
    Route("redis_configuration", "redis", redis_tools.configuration),
    Route("redis_acl_users", "redis", redis_tools.acl_users),
    Route("redis_acl_permissions", "redis", redis_tools.acl_permissions, (P("user", required=True),)),
    Route("redis_keyspace_overview", "redis", redis_tools.keyspace_overview),
    Route("redis_persistence_configuration", "redis", redis_tools.persistence_configuration),
    # postgresql per-database catalog
    Route("pg_list_schemas", "postgresql", pg_catalog.list_schemas, (P("database", required=True), _limit(100))),
    Route("pg_list_tables", "postgresql", pg_catalog.list_tables, (P("database", required=True), P("schema", default=""), _limit())),
    Route("pg_describe_table", "postgresql", pg_catalog.describe_table,
          (P("database", required=True), P("schema", required=True), P("table", required=True))),
    Route("pg_list_indexes", "postgresql", pg_catalog.list_indexes,
          (P("database", required=True), P("schema", default=""), P("table", default=""), _limit())),
    Route("pg_role_privileges", "postgresql", pg_catalog.role_privileges, (P("role", required=True), P("database", default=""))),
)}


def parse_args(route: Route, query: dict[str, list[str]]) -> dict:
    known = {p.name for p in route.params}
    unknown = sorted(set(query) - known)
    if unknown:
        raise invalid(f"unknown parameter(s): {', '.join(unknown)}")
    args: dict[str, Any] = {}
    for p in route.params:
        values = query.get(p.name)
        raw = values[-1] if values else None
        if raw is None or raw == "":
            if p.required:
                raise invalid(f"{p.name} is required")
            args[p.name] = p.default
            continue
        if len(raw) > 128:
            raise invalid(f"{p.name} is too long")
        if p.kind == "int":
            try:
                v = int(raw)
            except ValueError:
                raise invalid(f"{p.name} must be an integer") from None
            if (p.min is not None and v < p.min) or (p.max is not None and v > p.max):
                raise invalid(f"{p.name} must be between {p.min} and {p.max}")
            args[p.name] = v
        else:
            args[p.name] = raw
    return args


class Adapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.slots = threading.BoundedSemaphore(catalog()["limits"]["adapter_max_concurrency"])

    def call(self, tool: str, query: dict[str, list[str]]) -> dict:
        request_id = uuid.uuid4().hex[:16]
        route = ROUTES.get(tool)
        t0 = time.perf_counter()
        env: dict[str, Any] = {"tool": tool, "request_id": request_id}
        args: dict = {}
        try:
            if route is None:
                raise ToolError("not_found", "unknown_tool", "No such tool on this adapter")
            env["engine"] = route.engine
            args = parse_args(route, query)
            if not self.slots.acquire(blocking=False):
                raise ToolError("busy", "concurrency_limit", "Too many concurrent calls; retry shortly")
            try:
                env["data"] = route.fn(self.settings, args)
            finally:
                self.slots.release()
            env["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - every failure becomes a status
            e = classify(exc)
            if e.code == "internal_error":
                log.exception("internal error in %s", tool)
            else:
                log.info("%s -> %s (%s)", tool, e.code, type(exc).__name__)
            env["status"] = e.status
            env["error"] = {"code": e.code, "message": e.message}
        env = {"status": env.pop("status"), **env}
        audit.record({
            "request_id": request_id, "tool": tool, "args": args, "status": env["status"],
            "error_code": env.get("error", {}).get("code"),
            "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
        })
        return env


def make_handler(adapter: Adapter):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"dba-observer-adapter/{VERSION}"
        sys_version = ""

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._send(200, {"status": "ok", "version": VERSION})
            if url.path.startswith("/v1/tools/"):
                tool = url.path.removeprefix("/v1/tools/")
                return self._send(200, adapter.call(tool, parse_qs(url.query, keep_blank_values=True)))
            return self._send(404, {"status": "not_found"})

        def log_message(self, fmt, *args):  # access log is replaced by the audit line
            pass

    return Handler


def serve(settings: Settings) -> None:
    adapter = Adapter(settings)
    httpd = ThreadingHTTPServer((settings.listen_host, settings.listen_port), make_handler(adapter))
    httpd.daemon_threads = True
    log.info("dba-observer-adapter %s listening on %s:%s", VERSION, settings.listen_host, settings.listen_port)
    httpd.serve_forever()
