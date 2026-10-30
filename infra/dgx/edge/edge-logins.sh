#!/usr/bin/env bash
# Create the edge database logins and the empty crv-agent-memory database on the restored DGX
# PostgreSQL (after restore.sh and verify-staging.sh pass). Generates one password file per login
# on the encrypted volume (0600, never printed) and passes them to edge-logins.sql through the
# container environment, never through command arguments.
#   CRV_SECURE        mounted encrypted volume (default /srv/crv-secure)
#   CRV_PG_CONTAINER  PostgreSQL container (default crv-pg16-staging-postgres-1)
# Then, as crv_edge_migrator, run `crv-agent setup-memory` and
# `crv-agent grant-memory-runtime --login crv_edge_agent` (see Docs/DGX_EDGE_RUNBOOK.md).
set -euo pipefail

secure="${CRV_SECURE:-/srv/crv-secure}"
container="${CRV_PG_CONTAINER:-crv-pg16-staging-postgres-1}"
secrets="$secure/secrets"

mountpoint -q "$secure" || { echo "edge-logins: $secure is not mounted (open the LUKS volume)" >&2; exit 1; }
[ -d "$secrets" ] || { echo "edge-logins: $secrets is missing" >&2; exit 1; }

for login in crv_edge_agent crv_edge_migrator; do
    file="$secrets/$login.password"
    if [ ! -s "$file" ]; then
        (umask 077 && head -c 32 /dev/urandom | base64 | tr -d '\n=+/' > "$file")
        echo "edge-logins: generated $file" >&2
    fi
    chmod 0600 "$file"
done

{ cat "$secrets/crv_edge_agent.password"; echo; cat "$secrets/crv_edge_migrator.password"; echo; } |
    docker exec -i "$container" sh -c '
        IFS= read -r agent && IFS= read -r migrator &&
        CRV_AGENT_PASSWORD="$agent" CRV_MIGRATOR_PASSWORD="$migrator" \
            exec psql -X -q -At -v ON_ERROR_STOP=1 -U postgres -d postgres \
                -f /migration-sql/edge-logins.sql'
