"""Redis tools.

Every tool issues a fixed command and returns an allowlisted projection of the
reply. Nothing reads a key, and nothing enumerates keys: no KEYS, no SCAN, no
MONITOR. The ACL user behind this (grants/redis-acl.txt) is the backstop, and
it cannot narrow CONFIG GET to parameter names, so the CONFIG_* allowlists here
are what keep `requirepass` and `masterauth` out of every reply.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import redis

from .config import Settings
from .errors import invalid, not_configured

_pool_lock = threading.Lock()
_pools: dict[tuple, redis.BlockingConnectionPool] = {}

# --- allowlists -------------------------------------------------------------

HEALTH_FIELDS = ("redis_version", "redis_mode", "uptime_in_seconds", "uptime_in_days", "process_supervised")
MEMORY_FIELDS = (
    "used_memory", "used_memory_human", "used_memory_rss", "used_memory_rss_human",
    "used_memory_peak", "used_memory_peak_human", "used_memory_peak_perc",
    "used_memory_dataset_perc", "maxmemory", "maxmemory_human", "maxmemory_policy",
    "mem_fragmentation_ratio", "mem_fragmentation_bytes", "allocator_frag_ratio",
    "lazyfree_pending_objects", "mem_allocator",
)
CLIENT_FIELDS = (
    "connected_clients", "blocked_clients", "tracking_clients", "pubsub_clients",
    "clients_in_timeout_table", "maxclients", "client_recent_max_input_buffer",
    "client_recent_max_output_buffer",
)
CLIENT_STATS_FIELDS = ("rejected_connections", "total_connections_received", "client_query_buffer_limit_disconnections", "client_output_buffer_limit_disconnections")
KEYSPACE_STATS_FIELDS = (
    "keyspace_hits", "keyspace_misses", "expired_keys", "expired_stale_perc",
    "expired_time_cap_reached_count", "evicted_keys", "evicted_clients",
    "total_commands_processed", "instantaneous_ops_per_sec",
)
PERSISTENCE_FIELDS = (
    "loading", "async_loading", "rdb_changes_since_last_save", "rdb_bgsave_in_progress",
    "rdb_last_save_time", "rdb_last_bgsave_status", "rdb_last_bgsave_time_sec",
    "rdb_saves", "aof_enabled", "aof_rewrite_in_progress", "aof_rewrite_scheduled",
    "aof_last_rewrite_time_sec", "aof_last_bgrewrite_status", "aof_last_write_status",
    "module_fork_in_progress",
)
REPLICATION_FIELDS = (
    "role", "connected_slaves", "master_failover_state", "master_repl_offset",
    "second_repl_offset", "repl_backlog_active", "repl_backlog_size",
    "master_link_status", "master_last_io_seconds_ago", "master_sync_in_progress",
    "slave_repl_offset", "slave_read_only", "slave_priority", "master_link_down_since_seconds",
)
# Replica entries (slave0 ...) are reduced to these keys.
REPLICA_ENTRY_FIELDS = ("ip", "port", "state", "offset", "lag")

CONFIG_FIELDS = (
    "maxmemory", "maxmemory-policy", "maxmemory-samples", "maxclients", "timeout",
    "tcp-keepalive", "databases", "hz", "io-threads", "lazyfree-lazy-eviction",
    "lazyfree-lazy-expire", "lazyfree-lazy-server-del", "slowlog-log-slower-than",
    "slowlog-max-len", "latency-monitor-threshold", "protected-mode", "port",
    "replica-read-only", "min-replicas-to-write", "min-replicas-max-lag",
    "notify-keyspace-events", "activedefrag", "busy-reply-threshold",
)
PERSISTENCE_CONFIG_FIELDS = (
    "save", "appendonly", "appendfsync", "aof-use-rdb-preamble", "auto-aof-rewrite-percentage",
    "auto-aof-rewrite-min-size", "rdbcompression", "rdbchecksum",
    "stop-writes-on-bgsave-error", "no-appendfsync-on-rewrite",
)

ACL_USER_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")
MAX_ACL_USERS = 100


# --- connection -------------------------------------------------------------

def client(s: Settings) -> redis.Redis:
    if not s.redis_configured:
        raise not_configured("Redis")
    key = (s.redis_host, s.redis_port, s.redis_username, s.redis_password, s.redis_tls)
    with _pool_lock:
        pool = _pools.get(key)
        if pool is None:
            kwargs: dict[str, Any] = dict(
                host=s.redis_host, port=s.redis_port,
                username=s.redis_username, password=s.redis_password,
                socket_timeout=2, socket_connect_timeout=2,
                decode_responses=True, max_connections=2, timeout=2,
                # No CLIENT SETINFO on connect: the ACL user is not granted it.
                lib_name=None, lib_version=None,
            )
            if s.redis_tls:
                kwargs["connection_class"] = redis.SSLConnection
            pool = redis.BlockingConnectionPool(**kwargs)
            _pools[key] = pool
    return redis.Redis(connection_pool=pool)


def _pick(src: dict, fields: tuple[str, ...]) -> dict:
    return {k: src[k] for k in fields if k in src}


def _config(r: redis.Redis, names: tuple[str, ...]) -> dict:
    out: dict[str, Any] = {}
    for name in names:
        got = r.config_get(name)
        # A pattern-free name returns at most itself; anything else is ignored.
        if name in got:
            out[name] = got[name]
    return out


# --- phase 1 ----------------------------------------------------------------

def health(s: Settings, _: dict) -> dict:
    r = client(s)
    t0 = time.perf_counter()
    r.ping()
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    server = r.info("server")
    repl = r.info("replication")
    return {"ping_ms": latency_ms, "role": repl.get("role"), **_pick(server, HEALTH_FIELDS)}


def memory(s: Settings, _: dict) -> dict:
    r = client(s)
    mem = _pick(r.info("memory"), MEMORY_FIELDS)
    stats = r.info("stats")
    mem["evicted_keys"] = stats.get("evicted_keys")
    used, limit = mem.get("used_memory"), mem.get("maxmemory")
    mem["maxmemory_utilization_pct"] = round(100 * used / limit, 1) if used and limit else None
    return mem


def clients(s: Settings, _: dict) -> dict:
    r = client(s)
    data = _pick(r.info("clients"), CLIENT_FIELDS)
    data.update(_pick(r.info("stats"), CLIENT_STATS_FIELDS))
    if "maxclients" not in data:
        data.update(_config(r, ("maxclients",)))
    return data


def _keyspace(r: redis.Redis) -> dict[str, dict]:
    raw = r.info("keyspace")
    out = {}
    for db, v in raw.items():
        if isinstance(v, dict) and re.fullmatch(r"db\d+", db):
            out[db] = {"keys": v.get("keys", 0), "expires": v.get("expires", 0), "avg_ttl_ms": v.get("avg_ttl", 0)}
    return out


def keyspace_stats(s: Settings, _: dict) -> dict:
    r = client(s)
    data = _pick(r.info("stats"), KEYSPACE_STATS_FIELDS)
    hits, misses = data.get("keyspace_hits", 0), data.get("keyspace_misses", 0)
    data["hit_ratio_pct"] = round(100 * hits / (hits + misses), 2) if hits + misses else None
    ks = _keyspace(r)
    data["total_keys"] = sum(v["keys"] for v in ks.values())
    data["total_keys_with_expiry"] = sum(v["expires"] for v in ks.values())
    return data


def persistence(s: Settings, _: dict) -> dict:
    r = client(s)
    data = _pick(r.info("persistence"), PERSISTENCE_FIELDS)
    if "rdb_last_save_time" in data:
        data["seconds_since_last_save"] = int(time.time()) - int(data["rdb_last_save_time"])
    return data


def replication(s: Settings, _: dict) -> dict:
    r = client(s)
    raw = r.info("replication")
    data = _pick(raw, REPLICATION_FIELDS)
    replicas = []
    for k, v in raw.items():
        if re.fullmatch(r"slave\d+", k) and isinstance(v, dict):
            replicas.append(_pick(v, REPLICA_ENTRY_FIELDS))
    data["replicas"] = replicas
    if data.get("role") == "master" and not replicas:
        data["replication"] = "not_configured"
    elif data.get("role") == "slave":
        data["replication"] = "replica"
    else:
        data["replication"] = "primary"
    return data


# --- phase 2 ----------------------------------------------------------------

def configuration(s: Settings, _: dict) -> dict:
    return _config(client(s), CONFIG_FIELDS)


def persistence_configuration(s: Settings, _: dict) -> dict:
    r = client(s)
    data = _config(r, PERSISTENCE_CONFIG_FIELDS)
    # Whether ACLs persist across restart: an aclfile, or ACLs in redis.conf.
    # Only the fact that one is set is returned, never the path.
    acl = r.config_get("aclfile")
    data["aclfile_configured"] = bool(acl.get("aclfile"))
    return data


def keyspace_overview(s: Settings, _: dict) -> dict:
    ks = _keyspace(client(s))
    rows = []
    for db, v in sorted(ks.items(), key=lambda kv: int(kv[0][2:])):
        pct = round(100 * v["expires"] / v["keys"], 1) if v["keys"] else None
        rows.append({"database": int(db[2:]), **v, "expiring_pct": pct})
    return {"databases": rows}


def _flags(info: dict) -> list[str]:
    return [str(f) for f in (info.get("flags") or [])]


def _getuser(r: redis.Redis, name: str) -> dict | None:
    info = r.acl_getuser(name)
    if not info:
        return None
    flags = _flags(info)
    passwords = info.get("passwords") or []
    return {
        "user": name,
        "enabled": "on" in flags or info.get("enabled") is True,
        "nopass": "nopass" in flags,
        "password_count": len(passwords),   # count only; the hashes never leave this function
        "flags": [f for f in flags if f not in ("on", "off")],
        "commands": info.get("commands"),
        "keys": info.get("keys"),
        "channels": info.get("channels"),
        "selectors": info.get("selectors") or [],
    }


def acl_users(s: Settings, _: dict) -> dict:
    r = client(s)
    names = sorted(r.acl_users())
    truncated = len(names) > MAX_ACL_USERS
    rows = []
    for name in names[:MAX_ACL_USERS]:
        u = _getuser(r, name)
        if u:
            rows.append({k: u[k] for k in ("user", "enabled", "nopass", "password_count")})
    return {"users": rows, "truncated": truncated}


def acl_permissions(s: Settings, args: dict) -> dict:
    name = args["user"]
    if not ACL_USER_RE.fullmatch(name):
        raise invalid("user must be 1-64 characters of letters, digits and _ . : @ -")
    u = _getuser(client(s), name)
    if u is None:
        from .errors import ToolError
        raise ToolError("not_found", "acl_user_not_found", "No ACL user with that name")
    return u
