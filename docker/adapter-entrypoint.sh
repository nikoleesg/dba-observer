#!/bin/sh
# Adapter container entrypoint: load the mounted secret file, then exec the adapter.
# The adapter's file holds the Redis and PostgreSQL observer credentials only;
# MySQL's is mounted into the Toolbox container alone.
set -eu

SECRET="${DBA_OBSERVER_SECRET_FILE:-/run/secrets/dba-observer-adapter.env}"
if [ ! -r "$SECRET" ]; then
  echo "FATAL: secret file $SECRET is missing or unreadable (must be readable by uid 1608)." >&2
  exit 1
fi
set -a
. "$SECRET"
set +a

exec python -m dba_observer
