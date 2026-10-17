#!/usr/bin/env bash
# Refuse any tools.yaml that could run caller-supplied SQL or commands.
# Runs at image build and again at every container start.
set -euo pipefail

FILE="${1:?usage: check-tools-yaml <tools.yaml>}"

# The only types this server may use: three source kinds and three tool kinds.
ALLOWED='^(mysql|postgres|http|mysql-sql|postgres-sql)$'

bad=0
while read -r t; do
  if ! [[ "$t" =~ $ALLOWED ]]; then
    echo "FATAL: forbidden type '$t' in $FILE (allowed: mysql, postgres, http, mysql-sql, postgres-sql)" >&2
    bad=1
  fi
done < <(sed -n 's/^type:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$FILE" | sort -u)

# templateParameters splice text into SQL; this server binds values only.
if grep -qE '^[[:space:]]*templateParameters:' "$FILE"; then
  echo "FATAL: templateParameters found in $FILE; this server binds parameters, never splices them" >&2
  bad=1
fi

[ "$bad" -eq 0 ] || exit 1
echo "check-tools-yaml: ok ($(grep -c '^kind: tool$' "$FILE") tools, types: $(sed -n 's/^type:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$FILE" | sort -u | tr '\n' ' '))"
