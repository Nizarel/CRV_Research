#!/usr/bin/env bash
# DGX migration rehearsal with synthetic data only. Builds a throwaway PostgreSQL 16 "source"
# shaped like the CRV ingestion-catalog (AGE baseline and every migration, crv_retrieval rows with
# 3,072-dimension vectors and the HNSW index, two AGE graphs, objects owned by a NOLOGIN stand-in
# for the Entra admin, database grants and settings), exports it with export.sh in every mode,
# restores the schema and full exports with restore.sh into a throwaway target, shows the AGE
# graph id failure and its repair, compares manifests and runs retrieval-verify as a worker login,
# then fills embedding_m3 from a deterministic stub embedder (retrieval-embed), builds the m3
# index and searches it. The pipeline image must include retrieval-embed (2026-10-28 or later).
# Containers sit on an internal Docker network with no published port; they are removed on exit
# (KEEP=1 keeps them). Evidence: $REHEARSAL_ROOT/<run>/ (default ~/crv-migration/rehearsal).
#   CRV_PG_IMAGE=crv-postgres16:age1.6.0-pgvector0.8.2 CRV_PIPELINE_IMAGE=crv-pipeline:<tag> \
#       bash infra/dgx/staging/synthetic-rehearsal.sh
set -euo pipefail
umask 077
export LC_ALL=C

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo="$(cd "$here/../../.." && pwd -P)"
pg_image="${CRV_PG_IMAGE:?set CRV_PG_IMAGE}"
pipeline_image="${CRV_PIPELINE_IMAGE:?set CRV_PIPELINE_IMAGE}"
run="r$(date -u +%Y%m%d%H%M%S)"
work="${REHEARSAL_ROOT:-$HOME/crv-migration/rehearsal}/$run"
network="crv-rehearsal-$run"
source_ctr="crv-rehearsal-source-$run"
target_ctr="crv-rehearsal-target-$run"
database=ingestion-catalog
graph=crv_research_rehearsal

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() {
    log "REHEARSAL FAILED: $*"
    exit 1
}
psql_on() {
    local container="$1"
    shift
    docker exec -i "$container" psql -X -q -At -v ON_ERROR_STOP=1 -U postgres "$@"
}
cleanup() {
    if [ "${KEEP:-0}" != 1 ]; then
        docker rm -f "$source_ctr" "$target_ctr" > /dev/null 2>&1 || true
        docker network rm "$network" > /dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

start_pg() {
    local name="$1"
    shift
    # Throwaway password: every connection below uses the container's local trust rules.
    docker run -d --name "$name" --network "$network" --shm-size 1g \
        -e POSTGRES_PASSWORD="$(head -c 24 /dev/urandom | base64)" \
        -e POSTGRES_INITDB_ARGS="--data-checksums --encoding=UTF8 --locale-provider=libc --locale=en_US.utf8" \
        "$@" "$pg_image" postgres -c shared_preload_libraries=age,pg_stat_statements \
        -c jit=off -c random_page_cost=2 -c default_toast_compression=lz4 -c timezone=UTC \
        -c maintenance_work_mem=1GB -c max_parallel_maintenance_workers=4 > /dev/null
    # The init-time server listens on the socket only: TCP answers once the real one is up.
    for _ in $(seq 1 90); do
        if docker exec "$name" pg_isready -q -h 127.0.0.1 -U postgres; then
            return 0
        fi
        sleep 2
    done
    docker logs "$name" >&2
    die "$name did not start"
}

cypher_count() {
    psql_on "$1" -d "$database" <<SQL
SET search_path = ag_catalog, "\$user", public;
SELECT n FROM cypher('$graph', \$\$
    MATCH (:Document)-[:ABOUT_SITE]->(:Site)-[:OPERATED_BY]->(:Organisation) RETURN count(*)
\$\$) AS (n agtype);
SQL
}

export_bundle() {
    local mode="$1"
    mkdir -p "$work/bundles/$mode"
    log "export.sh MODE=$mode"
    docker run --rm --network "container:$source_ctr" --user "$(id -u):$(id -g)" \
        -v "$repo/infra/sql/migration:/sql:ro" \
        -v "$repo/infra/kubernetes/pipeline/db-export:/db-export:ro" \
        -v "$work/bundles/$mode:/work/out" \
        -e MODE="$mode" -e DATABASE="$database" -e RUN_ID="$run" -e SETTLE_SECONDS=5 \
        -e SQL_DIR=/sql -e PGHOST=127.0.0.1 -e PGUSER=postgres -e PGSSLMODE=disable \
        -e PGAPPNAME=crv-db-export -e HOME=/tmp \
        --entrypoint bash "$pg_image" /db-export/export.sh > "$work/logs/export-$mode.out" \
        || die "export.sh MODE=$mode failed"
}

restore_sh() {
    CRV_PG_CONTAINER="$target_ctr" CRV_BUNDLE_ROOT="$work/bundles" CRV_RESTORE_LOGS="$work/logs" \
        bash "$here/restore.sh" "$@"
}

mkdir -p "$work/bundles" "$work/logs"
log "run $run, evidence in $work"
docker network create --internal "$network" > /dev/null
start_pg "$source_ctr"
start_pg "$target_ctr" -v "$work/bundles:/bundle:ro" -v "$repo/infra/sql/migration:/migration-sql:ro"

log "source: database owned by a NOLOGIN stand-in for the Entra admin"
psql_on "$source_ctr" -d postgres <<'SQL'
CREATE ROLE "id-crv-aks-workload" NOLOGIN;
CREATE DATABASE "ingestion-catalog" OWNER "id-crv-aks-workload" TEMPLATE template0
    ENCODING 'UTF8' LOCALE_PROVIDER libc LOCALE 'en_US.utf8';
SQL

log "source: baseline, migrations and ${SEED_ROWS:-2000} synthetic chunks"
docker run --rm --network "container:$source_ctr" \
    -v "$repo/infra/sql/ingestion-catalog:/baseline:ro" -v "$here:/rehearsal:ro" \
    -e TIZ_DB__DSN="postgresql://postgres@127.0.0.1:5432/$database" \
    -e BASELINE_SQL=/baseline/0002_knowledge_prerequisites.sql -e SEED_ROWS="${SEED_ROWS:-2000}" \
    --entrypoint python "$pipeline_image" /rehearsal/synthetic_seed.py > "$work/logs/seed.json" \
    || die "seeding failed"
cat "$work/logs/seed.json" >&2

log "source: AGE graph, ownership, database grants and settings"
psql_on "$source_ctr" -d "$database" <<SQL
SET search_path = ag_catalog, "\$user", public;
SELECT create_graph('$graph');
SELECT * FROM cypher('$graph', \$\$
    CREATE (crv:Organisation {name: 'CRV'}), (partner:Organisation {name: 'Partner'}),
        (bleida:Site {name: 'Bleida'}), (bouskour:Site {name: 'Bou Skour'}),
        (bleida)-[:OPERATED_BY]->(crv), (bouskour)-[:OPERATED_BY]->(partner),
        (:Document {code: 'RA-2026-001'})-[:ABOUT_SITE]->(bleida),
        (:Document {code: 'RA-2026-002'})-[:ABOUT_SITE]->(bleida),
        (:Document {code: 'RA-2026-003'})-[:ABOUT_SITE]->(bouskour)
\$\$) AS (result agtype);
RESET search_path;
-- Everything the migrations and graphs created belongs to the admin stand-in, as on Azure;
-- ag_catalog (created by CREATE EXTENSION age, though not an extension member) stays as is.
DO \$owner\$
DECLARE
    statement text;
BEGIN
    FOR statement IN
        WITH owned AS (
            SELECT n.oid, n.nspname FROM pg_namespace AS n
            WHERE n.nspname !~ '^pg_'
                AND n.nspname NOT IN ('information_schema', 'public', 'ag_catalog')
                AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.deptype = 'e'
                    AND d.classid = 'pg_namespace'::regclass AND d.objid = n.oid)
        )
        SELECT format('ALTER SCHEMA %I OWNER TO "id-crv-aks-workload"', nspname) FROM owned
        UNION ALL
        SELECT format('ALTER %s %s OWNER TO "id-crv-aks-workload"',
            CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END, c.oid::regclass)
        FROM pg_class AS c
        WHERE c.relnamespace IN (SELECT oid FROM owned) AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
            AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid AND d.deptype IN ('a', 'i', 'e'))
        UNION ALL
        SELECT format('ALTER ROUTINE %s OWNER TO "id-crv-aks-workload"', p.oid::regprocedure)
        FROM pg_proc AS p
        WHERE p.pronamespace IN (SELECT oid FROM owned) AND p.prokind IN ('f', 'p')
            AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.classid = 'pg_proc'::regclass
                AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE statement;
    END LOOP;
END
\$owner\$;
REVOKE TEMPORARY ON DATABASE "ingestion-catalog" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE "ingestion-catalog" TO crv_kg_reader;
ALTER DATABASE "ingestion-catalog" SET search_path = "\$user", public, ag_catalog;
ALTER DATABASE "ingestion-catalog" SET default_text_search_config = 'pg_catalog.english';
ALTER ROLE "id-crv-aks-workload" IN DATABASE "ingestion-catalog" SET statement_timeout = '15min';
ANALYZE;
SQL
source_count="$(cypher_count "$source_ctr")"
[ "$source_count" = 3 ] || die "source Cypher count is '$source_count', expected 3"

export_bundle inventory
export_bundle schema
export_bundle full

log "restore: schema-only export into rehearsal_schema"
restore_sh restore "$work/bundles/schema/$database" rehearsal_schema || die "schema restore failed"

log "restore: full export without the AGE repair"
SKIP_AGE_REPAIR=1 restore_sh restore "$work/bundles/full/$database" || die "full restore failed"
stale="$(psql_on "$target_ctr" -d "$database" \
    -c 'SELECT count(*) FROM ag_catalog.ag_graph WHERE graphid <> namespace::oid')"
[ "$stale" -gt 0 ] || die "no stale AGE graph id after the restore: the repair is not exercised"
if cypher_count "$target_ctr" > "$work/logs/cypher-before-repair.out" 2>&1; then
    before="succeeded"
    log "note: Cypher reads still worked with the stale graph ids; the repair restores the" \
        "graphid = namespace OID invariant that new graphs and labels rely on"
else
    before="failed"
    log "Cypher failed before the repair: $(tail -n 1 "$work/logs/cypher-before-repair.out")"
fi
psql_on "$target_ctr" -d "$database" -f /migration-sql/age-graphid-repair.sql \
    > "$work/logs/age-repair.json" || die "AGE repair failed"
after_count="$(cypher_count "$target_ctr")" || die "Cypher still fails after the repair"
[ "$after_count" = "$source_count" ] || die "Cypher count after the repair is '$after_count'"
docker exec "$target_ctr" vacuumdb -q -U postgres -d "$database" --analyze --jobs 4

log "compare: full manifest and chunk list"
restore_sh compare "$work/bundles/full/$database" > "$work/logs/compare-full.json" \
    || die "the full restore differs from the source"

log "retrieval-verify as a worker login on the target"
psql_on "$target_ctr" -d postgres \
    -c 'CREATE ROLE crv_rehearsal_worker LOGIN IN ROLE tizert_ingest_worker, crv_kg_reader'
docker run --rm --network "container:$target_ctr" \
    -e TIZ_DB__DSN="postgresql://crv_rehearsal_worker@127.0.0.1:5432/$database" \
    -e TIZ_SEARCH__INDEX_NAME=crv-evidence-v1 \
    "$pipeline_image" retrieval-verify preflight parity recall explain --release crv-v1 \
    --sample 20 > "$work/logs/retrieval-verify.json" || die "retrieval-verify failed"

log "m3: retrieval-embed from a deterministic stub embedder, index, search"
# Stub: proves the procedure (embed, cache, guarded writes, index, search), not model quality.
m3_stub="crv-rehearsal-m3-$run"
docker run -d --rm --name "$m3_stub" --network "container:$target_ctr" --entrypoint python \
    "$pipeline_image" -c '
import hashlib, math
from aiohttp import web
def vector(text):
    block, out = hashlib.sha256(text.encode()).digest(), []
    while len(out) < 1024:
        block = hashlib.sha256(block).digest()
        out.extend((b - 127.5) / 127.5 for b in block)
    norm = math.sqrt(sum(x * x for x in out[:1024]))
    return [x / norm for x in out[:1024]]
async def embeddings(request):
    body = await request.json()
    return web.json_response({"data": [{"index": i, "embedding": vector(t)}
                                       for i, t in enumerate(body["input"])]})
app = web.Application()
app.router.add_post("/v1/embeddings", embeddings)
web.run_app(app, host="127.0.0.1", port=8099, print=None)
' > /dev/null
embed_m3() {
    docker run --rm --network "container:$target_ctr" \
        -e TIZ_DB__DSN="postgresql://crv_rehearsal_worker@127.0.0.1:5432/$database" \
        -e TIZ_LOCAL_EMBEDDINGS__URL=http://127.0.0.1:8099/v1 \
        -e TIZ_LOCAL_EMBEDDINGS__MODEL=crv-rehearsal-stub-m3 \
        "$pipeline_image" retrieval-embed --release crv-v1 --batch-rows 500
}
for _ in $(seq 1 30); do
    docker exec "$target_ctr" bash -c 'exec 3<>/dev/tcp/127.0.0.1/8099' 2> /dev/null && break
    sleep 1
done
embed_m3 > "$work/logs/m3-embed.json" || die "retrieval-embed failed"
embed_m3 > "$work/logs/m3-embed-rerun.json" || die "retrieval-embed rerun failed"
docker rm -f "$m3_stub" > /dev/null
jq -e '.failed == 0 and .after.m3 == .after.chunks and .after.chunks > 0' \
    "$work/logs/m3-embed.json" > /dev/null || die "m3 vectors are incomplete"
jq -e '.selected == 0 and .written == 0' "$work/logs/m3-embed-rerun.json" > /dev/null \
    || die "a second retrieval-embed run was not a no-op"
docker run --rm --network "container:$target_ctr" \
    -e TIZ_DB__DSN="postgresql://postgres@127.0.0.1:5432/$database" \
    "$pipeline_image" retrieval-index --model m3 --m 16 --ef-construction 64 \
    --maintenance-work-mem 512MB --parallel-workers 0 > "$work/logs/m3-index.json" \
    || die "the m3 index build failed"
docker run --rm --network "container:$target_ctr" \
    -e TIZ_DB__DSN="postgresql://crv_rehearsal_worker@127.0.0.1:5432/$database" \
    "$pipeline_image" retrieval-verify recall explain --model m3 --release crv-v1 --sample 20 \
    > "$work/logs/m3-verify.json" || die "m3 retrieval-verify failed"
m3_match="$(psql_on "$target_ctr" -d "$database" <<'SQL'
WITH probe AS (
    SELECT content, embedding_m3::real[] AS vector FROM crv_retrieval.chunk
    WHERE release_id = 'crv-v1' ORDER BY projection_id LIMIT 1)
SELECT (SELECT s.content FROM crv_retrieval.search('crv-v1', 'zzzzqq', '{}'::text[], 'm3',
            probe.vector, 1) AS s) = probe.content
FROM probe;
SQL
)"
[ "$m3_match" = t ] || die "m3 search did not return the probed chunk first"

log "reader: search through the function, no direct table access"
hits="$(psql_on "$target_ctr" -d "$database" <<'SQL'
SET ROLE crv_kg_reader;
SELECT count(*) FROM crv_retrieval.search('crv-v1', 'flottation cuivre', '{}'::text[], NULL, NULL, 5);
SQL
)"
[ "$hits" -gt 0 ] || die "the reader found nothing through crv_retrieval.search"
if psql_on "$target_ctr" -d "$database" \
        -c 'SET ROLE crv_kg_reader' -c 'SELECT 1 FROM crv_retrieval.chunk LIMIT 1' \
        > /dev/null 2> "$work/logs/reader-denied.out"; then
    die "crv_kg_reader can read crv_retrieval.chunk"
fi

jq -n --arg run "$run" --arg before "$before" --arg hits "$hits" \
    --slurpfile seed "$work/logs/seed.json" \
    --slurpfile repair "$work/logs/age-repair.json" \
    --slurpfile compared "$work/logs/compare-full.json" \
    --slurpfile verify "$work/logs/retrieval-verify.json" \
    --slurpfile m3 "$work/logs/m3-embed.json" \
    --slurpfile m3_index "$work/logs/m3-index.json" \
    --slurpfile m3_verify "$work/logs/m3-verify.json" \
    '{run: $run, seed: $seed[0], stale_graph_ids_after_restore: ($repair[0].repaired),
      cypher_before_repair: $before, age_repair: $repair[0], compare: $compared[0],
      retrieval_verify: ($verify | map(select(type == "object" and has("preflight"))) | .[0]
          | {ok, parity: .parity.ok, recall: .recall, explain: .explain.ok}),
      m3: {embedded: $m3[0].embedded, written: $m3[0].written, status: $m3[0].after,
           index: $m3_index[0].index, rerun_noop: true, search_probe: "first",
           recall: ($m3_verify | map(select(type == "object" and has("recall"))) | .[0]
               | {ok, recall: .recall.sampled.recall, explain: .explain.ok})},
      reader_hits: ($hits | tonumber), reader_table_access: "denied"}' | tee "$work/summary.json"
log "REHEARSAL PASSED"
