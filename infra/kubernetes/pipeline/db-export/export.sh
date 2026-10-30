#!/usr/bin/env bash
# CRV db-export, export step. Runs in the PostgreSQL 16 client image of the db-export Job (or in
# a rehearsal container) and writes OUT_ROOT/DATABASE/ for upload.py:
#   MODE=inventory  read-only evidence (inventory.jsonl, also printed to the log)
#   MODE=schema     inventory, catalog manifest and a schema-only dump
#   MODE=full       freeze check, inventory, manifest, chunk list, full dump, freeze check again
# Connection settings come from the libpq environment (PGHOST, PGUSER, PGPASSFILE, PGSSLMODE...).
# Exit 4: tables or sequences changed in the settle period before the export; exit 5: they changed
# while it ran. SHA256SUMS and DONE are written last, only after every step succeeded.
set -euo pipefail
umask 077
export LC_ALL=C

MODE="${MODE:?MODE is required}"
DATABASE="${DATABASE:?DATABASE is required}"
RUN_ID="${RUN_ID:?RUN_ID is required}"
RELEASE="${RELEASE:-crv-v1}"
SETTLE_SECONDS="${SETTLE_SECONDS:-60}"
SQL_DIR="${SQL_DIR:-/scripts}"
OUT="${OUT_ROOT:-/work/out}/$DATABASE"

case "$MODE" in
    inventory | schema | full) ;;
    *) echo "MODE must be inventory, schema or full" >&2; exit 2 ;;
esac
if [[ ! "$DATABASE" =~ ^[a-z][a-z0-9_-]{0,62}$ ]]; then
    echo "Unexpected DATABASE: $DATABASE" >&2
    exit 2
fi
if [[ ! "$SETTLE_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "SETTLE_SECONDS must be a number of seconds" >&2
    exit 2
fi

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
run_sql() { psql -X -q -At -v ON_ERROR_STOP=1 -d "$DATABASE" "$@"; }

# Cumulative insert/update/delete counters of every table and every sequence position. Statistics
# reach pg_stat_user_tables within seconds of a commit, so compare after a settle period.
freeze_check() { run_sql -f "$SQL_DIR/freeze-check.sql" > "freeze-$1.tsv"; }
unchanged() {
    if cmp -s "freeze-$1.tsv" "freeze-$2.tsv"; then
        return 0
    fi
    diff "freeze-$1.tsv" "freeze-$2.tsv" >&2 || true
    return 1
}

mkdir -p "$OUT"
if [ -n "$(ls -A "$OUT")" ]; then
    echo "$OUT is not empty: use a new RUN_ID" >&2
    exit 2
fi
cd "$OUT"

{
    printf 'run_id=%s mode=%s database=%s release=%s\n' "$RUN_ID" "$MODE" "$DATABASE" "$RELEASE"
    pg_dump --version
    pg_restore --version
    psql --version
} > tools.txt

if [ "$MODE" = full ]; then
    log "freeze check: write counters now and after ${SETTLE_SECONDS}s"
    freeze_check a
    sleep "$SETTLE_SECONDS"
    freeze_check b
    if ! unchanged a b; then
        log "a writer changed $DATABASE during the settle period; nothing was exported"
        exit 4
    fi
fi

log "inventory"
run_sql -v release="$RELEASE" -f "$SQL_DIR/inventory.sql" > inventory.jsonl
if [ "$MODE" = inventory ]; then
    cat inventory.jsonl
else
    catalog_only=0
    scope=()
    if [ "$MODE" = schema ]; then
        catalog_only=1
        scope=(--schema-only)
    fi
    log "manifest (catalog_only=$catalog_only)"
    run_sql -v catalog_only="$catalog_only" -f "$SQL_DIR/manifest.sql" > manifest.jsonl
    if [ "$MODE" = full ]; then
        log "chunk list"
        run_sql -f "$SQL_DIR/chunks.sql" > chunks.tsv
    fi
    dump="$DATABASE.$MODE.dump"
    # Owners and grants are kept: the target restores them onto NOLOGIN placeholder roles.
    log "pg_dump $MODE to $dump"
    pg_dump --format=custom --compress=zstd --lock-wait-timeout=10s --no-publications \
        --no-subscriptions "${scope[@]}" --file="$dump" --dbname="$DATABASE"
    pg_restore --list "$dump" > "$DATABASE.$MODE.toc"
fi

if [ "$MODE" = full ]; then
    log "freeze check after ${SETTLE_SECONDS}s"
    sleep "$SETTLE_SECONDS"
    freeze_check c
    if ! unchanged b c; then
        log "a writer changed $DATABASE during the export; discard this run"
        exit 5
    fi
fi

files=()
for name in *; do
    [ "$name" = SHA256SUMS ] || files+=("$name")
done
sha256sum -- "${files[@]}" > SHA256SUMS
sha256sum --check --strict --quiet SHA256SUMS
: > DONE
stat --format='%s %n' -- "${files[@]}"
cat SHA256SUMS
log "export complete: $OUT"
