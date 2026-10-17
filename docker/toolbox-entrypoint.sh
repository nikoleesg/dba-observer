#!/usr/bin/env bash
# Toolbox container entrypoint: load the mounted secret file, re-check the tool
# config, then exec Toolbox.
set -euo pipefail

SECRET="${DBA_OBSERVER_SECRET_FILE:-/run/secrets/dba-observer-toolbox.env}"
if [ ! -r "$SECRET" ]; then
  echo "FATAL: secret file $SECRET is missing or unreadable (must be readable by uid 1607)." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
. "$SECRET"
set +a
: "${MYSQL_HOST:?missing from $SECRET}"
: "${MYSQL_USER:?missing from $SECRET}"
: "${MYSQL_PASSWORD:?missing from $SECRET}"
: "${PG_HOST:?missing from $SECRET}"
: "${PG_USER:?missing from $SECRET}"
: "${PG_PASSWORD:?missing from $SECRET}"
: "${PG_HOME_DATABASE:?missing from $SECRET}"

CONFIG=/etc/dba-observer/tools.yaml
check-tools-yaml "$CONFIG"

# -a 0.0.0.0: Toolbox defaults to loopback, which is unreachable from Traefik.
# --disable-reload: the config is baked into a read-only image.
# --logging-format JSON + --log-level DEBUG: the audit trail. At INFO Toolbox logs
#   only "POST /mcp => 200"; at DEBUG it adds, per call, "tool name: <tool>" and
#   "invocation params: [...]" under one trace id. Verified on v1.14.0 that DEBUG
#   logs neither SQL text, result rows nor credentials.
# --allowed-hosts: DNS-rebinding guard; set to the public hostname in deployment.
#   `localhost` is always added when a list is set: the image HEALTHCHECK sends
#   `Host: localhost` from inside the container, and is refused (403) otherwise.
ALLOWED_HOSTS="${TOOLBOX_ALLOWED_HOSTS:-*}"
if [ "$ALLOWED_HOSTS" != "*" ]; then
  ALLOWED_HOSTS="$ALLOWED_HOSTS,localhost"
fi
exec toolbox --config "$CONFIG" --disable-reload -a 0.0.0.0 -p 5000 \
  --logging-format JSON --log-level "${TOOLBOX_LOG_LEVEL:-DEBUG}" \
  --allowed-hosts "$ALLOWED_HOSTS"
