# dba-observer

A read-only DBA MCP server for the homelab `db` VM's **MySQL**, **PostgreSQL** and **Redis**.
It answers monitoring, schema, configuration, privilege and incident-diagnosis questions with
**fixed, reviewed queries**. It never runs caller-supplied SQL or Redis commands, never reads an
application row or Redis key, and never returns a credential.

It covers Phases 0–3 (foundation, monitoring, inspection, diagnostics) of the DBA MCP
requirements. Phase 4, approval-gated administration, is a separate server, `dba-admin`, with its
own credentials. This one has no mutating tool at all.

- **Version:** 0.1.0 · **Transport:** streamable HTTP (`:5000/mcp`) · **Language:** Python adapter + YAML-configured Toolbox
- **Built on:** [Google MCP Toolbox for Databases](https://github.com/googleapis/mcp-toolbox)
  v1.14.0 (Apache 2.0), digest-pinned

## Architecture

```text
client ─► Cloudflare Access / MCP portal ─► Traefik ─► dba-observer (Toolbox, :5000/mcp)
                                                        ├─ mysql-sql / postgres-sql ─► MySQL, PostgreSQL
                                                        └─ http ─► dba-observer-adapter (:8080, private network)
                                                                    └─► Redis, PostgreSQL per-database catalog
```

Two containers from one `Dockerfile`, one MCP endpoint:

| Container | Image target | Serves | Holds credentials for |
|---|---|---|---|
| `dba-observer` | `toolbox` | MCP; 29 SQL tools defined in `docker/tools.yaml`; proxies 21 tools to the adapter | MySQL, PostgreSQL (home database) |
| `dba-observer-adapter` | `adapter` | 21 tools, HTTP JSON, Toolbox only | Redis, PostgreSQL (catalog databases) |

**Why there is an adapter.** Toolbox is the framework, but two contracts it cannot meet:

- Its Redis tool returns raw replies, so it cannot drop password hashes from `ACL GETUSER` or
  allowlist `INFO` fields.
- A Toolbox PostgreSQL source is bound to **one** database, and schemas, tables and object grants
  are only visible from inside each database.

The adapter does exactly those (plus the five meta tools) and nothing else.

## Tools (50)

Every tool is annotated `readOnlyHint: true`. The full contract, including limits and
guarantees, is `src/dba_observer/catalog.yaml`. `get_capabilities` and `get_policy` serve it
at run time.

| Phase | MySQL | PostgreSQL | Redis | Server |
|---|---|---|---|---|
| **0 Foundation** | | | | `list_targets`, `get_capabilities`, `tool_health`, `get_policy`, `get_audit_status` |
| **1 Monitoring** | `mysql_health`, `mysql_connections`, `mysql_status`, `mysql_database_sizes`, `mysql_replication_status` | `pg_health`, `pg_connections`, `pg_database_sizes`, `pg_transaction_stats`, `pg_replication_status` | `redis_health`, `redis_memory`, `redis_clients`, `redis_keyspace_stats`, `redis_persistence`, `redis_replication_status` | |
| **2 Inspection** | `mysql_list_databases`, `mysql_list_tables`, `mysql_describe_table`, `mysql_list_indexes`, `mysql_configuration`, `mysql_list_users`, `mysql_user_grants` | `pg_list_databases`, `pg_configuration`, `pg_list_roles`, `pg_list_schemas`ᵃ, `pg_list_tables`ᵃ, `pg_describe_table`ᵃ, `pg_list_indexes`ᵃ, `pg_role_privileges`ᵃ | `redis_configuration`, `redis_acl_users`, `redis_acl_permissions`, `redis_keyspace_overview`, `redis_persistence_configuration` | |
| **3 Diagnostics** | `mysql_active_sessions`, `mysql_lock_waits`, `mysql_slow_query_summary`, `mysql_table_io_stats`, `mysql_innodb_metrics`, `mysql_table_fragmentation` | `pg_active_sessions`, `pg_blocking_sessions`, `pg_long_transactions` | | |

ᵃ Takes a `database` argument, which must be on the adapter's `PG_DATABASES` allowlist
(`list_targets` shows it). All Redis and Phase 0 tools are adapter tools too.

**Toolsets.** Toolbox also serves three subsets for clients with small context:
`/mcp/monitoring`, `/mcp/inspection` and `/mcp/diagnostics`.

**Result shapes.**
- SQL tools return rows.
- Adapter tools return `{"status", "tool", "engine", "request_id", "data" | "error"}`. `status` is
  one of `ok`, `unavailable`, `not_configured`, `unsupported`, `not_found`,
  `permission_denied`, `invalid_argument`, `busy` or `error`.
- "No replica" is reported as `replication_role` / `replication` = `not_configured`, never as a
  failure.

**Deferred** to a later 0.x:
- EXPLAIN tools. They take a statement, and MySQL requires `SELECT` on every table they touch.
- `pg_query_stats` (needs `pg_stat_statements`).
- Bloat, vacuum and index-usage estimates.
- Redis latency, slowlog, command stats, hotkey and client diagnostics.

## Security model

The tool set is the first boundary, and the grants are the backstop. Both are tested.

- **No passthrough.**
  - `docker/check-tools-yaml.sh` runs at image build and at every start. It refuses any `type:`
    other than `mysql`, `postgres`, `http`, `mysql-sql` and `postgres-sql` (so no
    `*-execute-sql`, no prebuilt kinds). It also refuses `templateParameters`.
  - Every parameter is a bound value. Every integer is bounded, and every `limit` is capped at
    ≤ 500.
- **No rows, no keys.**
  - The MySQL account has no `SELECT` on any application schema. It sees metadata via
    `REFERENCES` per schema.
  - The PostgreSQL role is `pg_monitor` and reads `pg_catalog` only.
  - The Redis ACL user has no key or channel patterns at all.
- **No credentials.**
  - `mysql.user` is granted by column, without `authentication_string`.
  - `pg_roles` masks passwords.
  - Redis ACL output keeps only a password *count*.
  - Redis `CONFIG GET` names are a fixed allowlist, because ACLs cannot restrict
    `CONFIG GET requirepass`.
  - Driver error text is never returned; errors map to fixed codes.
- **No literals.**
  - MySQL session and statement text is `performance_schema` `DIGEST_TEXT`, which already
    replaces literals with `?`.
  - PostgreSQL query text is normalised in SQL: quoted strings and numbers become `?`.
  - Both are truncated to 300 characters. Dollar-quoted PostgreSQL bodies are truncated but not
    normalised.
- **Bounded cost.**
  - Statement timeout is 5 s on both engines; PostgreSQL lock timeout is 1 s.
  - Every PostgreSQL session is read-only, set per connection and again on the role.
  - Redis socket timeout is 2 s, and the adapter runs at most 4 calls at once.
  - Connection caps sit on the accounts: MySQL 5, PostgreSQL 6. The Toolbox PostgreSQL pool is 3.
- **Audit.**
  - Toolbox runs with `--logging-format JSON --log-level DEBUG`. Per call it logs `tool name`
    and `invocation params` under one trace id, and, verified on v1.14.0, no SQL, rows or
    credentials.
  - The adapter writes one JSON audit line per call to stdout.
  - Caller identity lives in Cloudflare Access logs.
- **Access.**
  - The public route is behind Cloudflare Access.
  - LAN callers are not authenticated. This is an owner decision for the read-only tier;
    `dba-admin` must authenticate.
  - `--allowed-hosts` should be set to the public hostname (`TOOLBOX_ALLOWED_HOSTS`).

## Database accounts

These are proposals; the operator applies them. The test stack applies them verbatim:

| File | Account |
|---|---|
| `grants/mysql.sql` | `dba_observer`@`<DOCKER_VM_IP>`: `PROCESS`, `REPLICATION CLIENT`; `SELECT` on `performance_schema`; `SELECT, EXECUTE` on `sys`; column `SELECT` on `mysql.user` (no hash); `SELECT` on `mysql.db`/`tables_priv`/`global_grants`/`role_edges`; `REFERENCES` per application schema |
| `grants/postgresql.sql` | `dba_observer` role: `pg_monitor`, `CONNECTION LIMIT 6`, read-only defaults; an empty home database `dba_observer` (the `postgres` database is rejected over the network); `CONNECT` per catalog database; one `hostssl` `pg_hba` line |
| `grants/redis-acl.txt` | `dba-observer`: `-@all +ping +info +config\|get +acl\|users +acl\|getuser`, `resetkeys resetchannels` |

## Configuration

Each container reads one mounted secret file. Never compose `environment:`.

**`dba-observer`** (`/run/secrets/dba-observer-toolbox.env`, readable by uid 1607):

| Variable | Purpose |
|---|---|
| `MYSQL_HOST`, `MYSQL_PORT` (3306), `MYSQL_USER`, `MYSQL_PASSWORD` | MySQL observer account |
| `MYSQL_TLS` | Go driver `tls` value: `preferred` (default), `true`, `skip-verify`, `false` |
| `PG_HOST`, `PG_PORT` (5432), `PG_USER`, `PG_PASSWORD` | PostgreSQL observer role |
| `PG_HOME_DATABASE` | Database the cluster-wide tools connect to (`dba_observer`) |
| `PG_SSLMODE` | `require` (default) |
| `ADAPTER_URL` | `http://dba-observer-adapter:8080` (default) |
| `ADAPTER_CIDR` | Subnet of the private network shared with the adapter. Toolbox's SSRF guard blocks private addresses unless allowed (default `172.30.99.0/29`) |
| `TOOLBOX_ALLOWED_HOSTS` | DNS-rebinding guard, e.g. `dba-observer.mcp-server.cc.cd` (default `*`) |
| `TOOLBOX_LOG_LEVEL` | `DEBUG` (default; the audit trail) |

**`dba-observer-adapter`** (`/run/secrets/dba-observer-adapter.env`, readable by uid 1608):

| Variable | Purpose |
|---|---|
| `REDIS_HOST`, `REDIS_PORT` (6379), `REDIS_USERNAME`, `REDIS_PASSWORD`, `REDIS_TLS` (`false`) | Redis observer ACL user |
| `PG_HOST`, `PG_PORT`, `PG_USER`, `PG_PASSWORD`, `PG_SSLMODE`, `PG_HOME_DATABASE` | Same PostgreSQL role as above |
| `PG_DATABASES` | Comma-separated allowlist for the catalog tools. Empty means none |
| `MYSQL_HOST`, `MYSQL_PORT` | TCP reachability probe for `tool_health` only. The adapter has no MySQL credential |

## Build, test, run

```bash
uv venv -p 3.12 && uv pip install -e '.[dev]'
.venv/bin/pytest tests/test_catalog.py tests/test_adapter_unit.py   # no docker needed

tests/stack/up       # builds both images, starts MySQL 8.0 / PostgreSQL 16 (TLS) / Redis 7.0
.venv/bin/pytest     # all 32, including integration
tests/stack/down
```

The integration suite proves:
- the MCP surface is exactly the catalog;
- every tool succeeds and no tool changes state (row checksums, accounts, keys and ACLs compared
  before and after);
- no planted secret, password or literal reaches any output;
- the observer credentials themselves are refused rows, hashes, keys and writes;
- diagnostics see real long statements and lock waits;
- limits are enforced, and PostgreSQL connections use TLS within the pool cap.

Images (build from the `v<version>` tag only; the version lives in `pyproject.toml`):

```bash
docker build --target toolbox -t dba-observer-toolbox:0.1.0 .
docker build --target adapter -t dba-observer-adapter:0.1.0 .
```

Probe a deployment: `python tests/mcp_client.py http://<host>:5000/mcp list`.

## Deployment

Containerization belongs to the deployment. In the homelab it is its own stack on the
`docker` VM:

- both services on a private bridge network with a fixed subnet (`ADAPTER_CIDR`), no published
  ports;
- the Toolbox container also on Traefik's `edge` network, routed as
  `dba-observer.mcp-server.cc.cd` on the `tunnel` entrypoint;
- the adapter has no Traefik route;
- both run `read_only`, `cap_drop: ALL` and `no-new-privileges`.

## Client config

The server sits behind Cloudflare Access and is reached through the MCP server portal. A direct
client uses the URL below. Complete, copy-paste-ready document:

```json
{
  "mcpServers": {
    "dba-observer": {
      "type": "http",
      "url": "https://dba-observer.mcp-server.cc.cd/mcp"
    }
  }
}
```

For the local test stack use `http://127.0.0.1:15000/mcp`. For one toolset append its name,
e.g. `/mcp/monitoring`.

A correct connection lists **50 tools**, and none of them executes SQL.

## Maintenance

**Bumping Toolbox.** Check the [releases](https://github.com/googleapis/mcp-toolbox/releases).
Re-pin the `FROM` digest (multi-arch index) in `Dockerfile`, then run the full suite against the
stack. Re-verify that DEBUG logging still omits SQL and rows; the audit claim depends on it.

**Changing the tool surface.** Edit `catalog.yaml`, `docker/tools.yaml` and, for adapter tools,
`server.py` together; `tests/test_catalog.py` fails until they agree. A new or renamed tool is
a **minor** bump (0.x). A fix that keeps output shapes is a patch. Tag `v<version>` and build
from the tag.
