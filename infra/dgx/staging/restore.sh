#!/usr/bin/env bash
# Restore one CRV db-export bundle into a NEW database of the DGX PostgreSQL 16 staging container,
# then prove that it matches the source. Fail-closed: a checksum, extension, restore, AGE repair
# or manifest difference stops the run with a non-zero exit.
#   restore.sh restore <bundle-dir> [target-db]   placeholder roles, database, pg_restore, AGE
#                                                 graph id repair, ANALYZE, comparison
#   restore.sh compare <bundle-dir> [target-db]   comparison only
# <bundle-dir> holds one database's export (SHA256SUMS, inventory.jsonl, manifest.jsonl and
# <database>.<mode>.dump) below CRV_BUNDLE_ROOT, which the container mounts read-only at /bundle.
# target-db defaults to the source database name. Azure's own privilege changes on pg_catalog
# objects (grants to its platform roles) are left out: the target keeps PostgreSQL's defaults.
# Environment:
#   CRV_PG_CONTAINER    staging container (default crv-pg16-staging-postgres-1)
#   CRV_BUNDLE_ROOT     host directory mounted at /bundle (required)
#   CRV_RESTORE_LOGS    evidence root (default ~/crv-migration/restore-logs)
#   SKIP_EXTENSIONS     space-separated source extensions to leave out (Azure-only ones)
#   REPLACE_DATABASE=1  drop an existing target database first
#   SKIP_AGE_REPAIR=1   rehearsal only: restore without the AGE repair and stop before comparing
set -euo pipefail
umask 077
export LC_ALL=C

ALLOWED_EXTENSIONS=(age vector pg_trgm unaccent pg_stat_statements plpgsql)

die() {
    echo "restore.sh: $*" >&2
    exit 1
}
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

action="${1:-}"
bundle="${2:-}"
target="${3:-}"
case "$action" in
    restore | compare) ;;
    *) die "usage: restore.sh restore|compare <bundle-dir> [target-db]" ;;
esac
[ -d "$bundle" ] || die "bundle directory '$bundle' not found"
container="${CRV_PG_CONTAINER:-crv-pg16-staging-postgres-1}"
root="$(cd "${CRV_BUNDLE_ROOT:?set CRV_BUNDLE_ROOT}" && pwd -P)"
bundle="$(cd "$bundle" && pwd -P)"
if [ "$bundle" != "$root" ] && [[ "$bundle" != "$root"/* ]]; then
    die "$bundle is not below CRV_BUNDLE_ROOT ($root)"
fi
inner="/bundle${bundle#"$root"}"
skip_list="${SKIP_EXTENSIONS:-}"

psql_in() { docker exec -i "$container" psql -X -q -At -v ON_ERROR_STOP=1 -U postgres "$@"; }

for name in SHA256SUMS inventory.jsonl manifest.jsonl tools.txt; do
    [ -f "$bundle/$name" ] || die "$bundle/$name is missing"
done
log "verifying checksums"
(cd "$bundle" && sha256sum --check --strict --quiet SHA256SUMS) || die "checksum mismatch"
grep -q '^pg_dump (PostgreSQL) 16\.' "$bundle/tools.txt" || die "the dump was not made by pg_dump 16"

source_db="$(jq -r 'select(.kind == "server") | .database' "$bundle/inventory.jsonl")"
[ -n "$source_db" ] || die "inventory.jsonl has no server line"
target="${target:-$source_db}"
[[ "$target" =~ ^[A-Za-z0-9_-]{1,63}$ ]] || die "unexpected target database name '$target'"
shopt -s nullglob
dumps=("$bundle"/*.dump)
shopt -u nullglob
[ "${#dumps[@]}" -eq 1 ] || die "expected exactly one .dump file in $bundle"
dump="$(basename "${dumps[0]}")"
mode="${dump#"$source_db".}"
mode="${mode%.dump}"
case "$mode" in
    schema | full) ;;
    *) die "unexpected dump name $dump (want $source_db.schema.dump or $source_db.full.dump)" ;;
esac

logs="${CRV_RESTORE_LOGS:-$HOME/crv-migration/restore-logs}/$target-$action-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$logs"
log "source $source_db ($mode) -> $container/$target; evidence in $logs"

compare() {
    local catalog_only=1
    [ "$mode" = full ] && catalog_only=0
    log "comparing manifests (catalog_only=$catalog_only)"
    psql_in -d "$target" -v catalog_only="$catalog_only" -f /migration-sql/manifest.sql \
        > "$logs/target-manifest.jsonl"
    # Raw lines are compared, sorted; extensions deliberately left out are dropped from both.
    # shellcheck disable=SC2016 # a jq program, not shell expansions
    local filter='fromjson as $o | select($o.kind != "extension"
        or ($o.name | IN($skip | splits(" +")) | not))'
    jq -R -r --arg skip "$skip_list" "$filter" "$bundle/manifest.jsonl" | sort > "$logs/source-manifest.sorted"
    jq -R -r --arg skip "$skip_list" "$filter" "$logs/target-manifest.jsonl" | sort > "$logs/target-manifest.sorted"
    if ! diff "$logs/source-manifest.sorted" "$logs/target-manifest.sorted" > "$logs/manifest.diff"; then
        head -n 60 "$logs/manifest.diff" >&2
        die "the restored database differs from the source manifest (see $logs/manifest.diff)"
    fi
    if [ "$mode" = full ] && [ -f "$bundle/chunks.tsv" ]; then
        log "comparing the chunk list"
        psql_in -d "$target" -f /migration-sql/chunks.sql > "$logs/target-chunks.tsv"
        cmp -s "$bundle/chunks.tsv" "$logs/target-chunks.tsv" \
            || die "crv_retrieval.chunk differs from the source chunk list"
    fi
    jq -n --arg source "$source_db" --arg target "$target" --arg mode "$mode" \
        --arg lines "$(wc -l < "$logs/source-manifest.sorted")" \
        '{kind: "compared", source: $source, target: $target, mode: $mode,
          manifest_lines: ($lines | tonumber), identical: true}' | tee "$logs/compare.json"
}

if [ "$action" = compare ]; then
    compare
    exit 0
fi

jq -e -s '[.[] | select(.kind == "special_objects")][0]
    | .publications == 0 and .event_triggers == 0 and .large_objects == 0' \
    "$bundle/inventory.jsonl" > /dev/null || die "publications, event triggers or large objects are not supported"

log "checking the archive contents"
docker exec "$container" pg_restore --list "$inner/$dump" > "$logs/source.list"
if grep -En '^[0-9]+; [0-9]+ [0-9]+ (PUBLICATION|SUBSCRIPTION|EVENT TRIGGER|BLOB|BLOBS|LARGE OBJECT)( |$)' \
        "$logs/source.list" >&2; then
    die "the archive holds unsupported entries"
fi
cp "$logs/source.list" "$logs/restore.list"
while read -r extension; do
    if [[ " $skip_list " == *" $extension "* ]]; then
        log "leaving out extension $extension"
        sed -i -E "s/^([0-9]+; [0-9]+ [0-9]+ (EXTENSION|COMMENT) - (EXTENSION )?$extension )/;\1/" \
            "$logs/restore.list"
    elif [[ " ${ALLOWED_EXTENSIONS[*]} " != *" $extension "* ]]; then
        die "extension $extension is not available on the target (allow-list it or set SKIP_EXTENSIONS)"
    fi
done < <(sed -nE 's/^[0-9]+; [0-9]+ [0-9]+ EXTENSION - ([^ ]+) .*/\1/p' "$logs/source.list")
platform_acls="$(grep -cE '^[0-9]+; [0-9]+ [0-9]+ ACL pg_catalog ' "$logs/restore.list" || true)"
if [ "$platform_acls" -gt 0 ]; then
    log "leaving out $platform_acls privilege changes on pg_catalog objects"
    sed -i -E 's/^([0-9]+; [0-9]+ [0-9]+ ACL pg_catalog )/;\1/' "$logs/restore.list"
fi

if [ "${REPLACE_DATABASE:-0}" = 1 ]; then
    log "dropping the existing $target"
    docker exec "$container" dropdb -U postgres --if-exists --force "$target"
fi
log "preparing placeholder roles and the database"
psql_in -d postgres -v target_db="$target" -f /migration-sql/prepare-target.sql \
    < "$bundle/inventory.jsonl" > "$logs/prepare.out"

list="/tmp/crv-restore-$target.list"
docker cp "$logs/restore.list" "$container:$list"
log "pg_restore (single transaction)"
started=$(date +%s)
if ! docker exec "$container" pg_restore -U postgres -d "$target" --exit-on-error \
        --single-transaction --no-security-labels --verbose -L "$list" "$inner/$dump" \
        2> "$logs/restore.log"; then
    tail -n 30 "$logs/restore.log" >&2
    die "pg_restore failed (see $logs/restore.log)"
fi
restore_seconds=$(( $(date +%s) - started ))
docker exec "$container" rm -f "$list"

if [ "${SKIP_AGE_REPAIR:-0}" = 1 ]; then
    log "restored in ${restore_seconds}s; AGE repair skipped (rehearsal): not compared"
    exit 0
fi
log "AGE graph id repair"
psql_in -d "$target" -f /migration-sql/age-graphid-repair.sql | tee "$logs/age-repair.json"
if [ "$mode" = full ]; then
    log "vacuum and analyze"
    docker exec "$container" vacuumdb -U postgres -d "$target" --analyze --jobs 4 \
        > "$logs/vacuum.log" 2>&1 || die "vacuumdb failed (see $logs/vacuum.log)"
fi
log "restored in ${restore_seconds}s"
compare
