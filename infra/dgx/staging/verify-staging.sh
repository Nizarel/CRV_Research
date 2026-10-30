#!/usr/bin/env bash
# P6 acceptance checks on the restored DGX staging database (read-only apart from one local
# verification login): release counts and vector dimensions, index validity, reader privileges,
# Cypher on every AGE graph, and retrieval-verify (preflight, parity, recall, explain) run from the
# ARM64 pipeline image as a login holding the restored worker roles. Prints one JSON summary.
#   CRV_PIPELINE_IMAGE=crv-pipeline:4520b64-arm64 bash verify-staging.sh [database]
# Environment: CRV_PG_CONTAINER (default crv-pg16-staging-postgres-1), RELEASE (default crv-v1),
# VERIFY_SAMPLE (default 30), CRV_RESTORE_LOGS (evidence root, default ~/crv-migration/restore-logs).
set -euo pipefail
umask 077
export LC_ALL=C

database="${1:-ingestion-catalog}"
container="${CRV_PG_CONTAINER:-crv-pg16-staging-postgres-1}"
pipeline_image="${CRV_PIPELINE_IMAGE:?set CRV_PIPELINE_IMAGE}"
release="${RELEASE:-crv-v1}"
sample="${VERIFY_SAMPLE:-30}"
logs="${CRV_RESTORE_LOGS:-$HOME/crv-migration/restore-logs}/$database-verify-$(date -u +%Y%m%dT%H%M%SZ)"
login=crv_dgx_verify
mkdir -p "$logs"

die() {
    echo "verify-staging.sh: $*" >&2
    exit 1
}
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
psql_in() {
    docker exec -i "$container" psql -X -q -At -v ON_ERROR_STOP=1 -U postgres -d "$database" "$@"
}

log "integrity, indexes and privileges"
psql_in -v release="$release" > "$logs/checks.json" <<'SQL'
SELECT json_build_object(
    'release', :'release',
    'chunks', count(*),
    'te3_vectors', count(embedding_te3),
    'wrong_te3_dimensions',
        count(*) FILTER (WHERE embedding_te3 IS NOT NULL AND vector_dims(embedding_te3) <> 3072),
    'invalid_indexes', (SELECT count(*) FROM pg_index AS i
        WHERE NOT (i.indisvalid AND i.indisready)),
    'reader_schema_usage', has_schema_privilege('crv_kg_reader', 'crv_retrieval', 'USAGE'),
    'reader_search_execute', (SELECT bool_and(has_function_privilege('crv_kg_reader', p.oid,
            'EXECUTE'))
        FROM pg_proc AS p
        WHERE p.pronamespace = 'crv_retrieval'::regnamespace AND p.proname = 'search'),
    'reader_table_select', has_table_privilege('crv_kg_reader', 'crv_retrieval.chunk', 'SELECT'),
    'reader_table_write', has_table_privilege('crv_kg_reader', 'crv_retrieval.chunk',
        'INSERT, UPDATE, DELETE, TRUNCATE'),
    'public_table_grants', ARRAY(SELECT DISTINCT table_schema || '.' || table_name
        FROM information_schema.role_table_grants
        WHERE grantee = 'PUBLIC'
            AND table_schema !~ '^(pg_|information_schema$|ag_catalog$)'
        ORDER BY 1))
FROM crv_retrieval.chunk
WHERE release_id = :'release';
SQL
jq -e '.chunks > 0 and .chunks == .te3_vectors and .wrong_te3_dimensions == 0
    and .invalid_indexes == 0 and .reader_schema_usage and .reader_search_execute
    and (.reader_table_select | not) and (.reader_table_write | not)
    and (.public_table_grants | length) == 0' "$logs/checks.json" > /dev/null \
    || die "integrity or privilege check failed: $(cat "$logs/checks.json")"

log "Cypher on every AGE graph"
cypher_count() {
    psql_in <<SQL
SET search_path = ag_catalog, "\$user", public;
SELECT n FROM cypher('$1', \$\$ $2 \$\$) AS (n agtype);
SQL
}
graphs="$(psql_in -c 'SELECT name FROM ag_catalog.ag_graph ORDER BY name')"
: > "$logs/graphs.jsonl"
for graph in $graphs; do
    [[ "$graph" =~ ^[a-z0-9_]+$ ]] || die "unexpected graph name $graph"
    jq -n -c --arg graph "$graph" \
        --argjson cypher_vertices "$(cypher_count "$graph" 'MATCH (v) RETURN count(v)')" \
        --argjson cypher_edges "$(cypher_count "$graph" 'MATCH ()-[e]->() RETURN count(e)')" \
        --argjson table_vertices "$(psql_in -c "SELECT count(*) FROM \"$graph\"._ag_label_vertex")" \
        --argjson table_edges "$(psql_in -c "SELECT count(*) FROM \"$graph\"._ag_label_edge")" \
        '{graph: $graph, cypher_vertices: $cypher_vertices, cypher_edges: $cypher_edges,
          table_vertices: $table_vertices, table_edges: $table_edges}' >> "$logs/graphs.jsonl"
done
jq -s -e 'length > 0 and all(.cypher_vertices == .table_vertices
    and .cypher_edges == .table_edges)' "$logs/graphs.jsonl" > /dev/null \
    || die "Cypher counts differ from the label tables: $(cat "$logs/graphs.jsonl")"

log "retrieval-verify as $login"
psql_in <<SQL
DO \$login\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$login') THEN
        CREATE ROLE $login LOGIN;
    END IF;
END
\$login\$;
GRANT tizert_ingest_worker, crv_kg_reader TO $login;
SQL
docker run --rm --network "container:$container" \
    -e TIZ_DB__DSN="postgresql://$login@127.0.0.1:5432/$database" \
    -e TIZ_SEARCH__INDEX_NAME=crv-evidence-v1 \
    "$pipeline_image" retrieval-verify preflight parity recall explain --release "$release" \
    --sample "$sample" > "$logs/retrieval-verify.out" || verify_failed=1
grep '"preflight"' "$logs/retrieval-verify.out" | tail -n 1 > "$logs/retrieval-verify.json" || true
[ -s "$logs/retrieval-verify.json" ] || die "retrieval-verify printed no report (see $logs)"

jq -n --slurpfile checks "$logs/checks.json" --slurpfile graphs "$logs/graphs.jsonl" \
    --slurpfile verify "$logs/retrieval-verify.json" --arg logs "$logs" \
    '{checks: $checks[0], graphs: $graphs,
      retrieval_verify: ($verify[0] | {ok, parity: .parity, recall: .recall.sampled,
          explain_ok: .explain.ok, unscoped_indexes: .explain.unscoped.indexes}),
      evidence: $logs}' | tee "$logs/summary.json"
[ -z "${verify_failed:-}" ] || die "retrieval-verify reported a failure (see $logs/retrieval-verify.out)"
