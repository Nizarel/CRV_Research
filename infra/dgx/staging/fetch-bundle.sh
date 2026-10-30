#!/usr/bin/env bash
# Fetch one db-export bundle from the private Blob container onto the encrypted volume in parallel
# HTTP ranges, then verify SHA256SUMS before moving it into place. Every range resumes from the
# bytes it already holds, so a stall, a VPN drop or a rerun never discards downloaded data. A host
# restart can lose a range's last writes while its length survives, so a range written before the
# current boot first drops its last 64 MiB, and a complete file is skipped only if its checksum
# matches. The curl config carries the Authorization and x-ms-version headers, plus a connect-to
# line when the private endpoint is reached through an SSH reverse tunnel from the operator's VPN.
#   fetch-bundle.sh <run-id> <curl-config> [streams]
# Environment: CRV_BLOB_ACCOUNT (default stawmc6icu2kueq), CRV_BLOB_CONTAINER (default
# crv-migration), CRV_DATABASE (default ingestion-catalog), CRV_SECURE (default /srv/crv-secure),
# CRV_FETCH_CHUNK (bytes per request, default 1 MiB), CRV_FETCH_LIMIT_RATE (curl --limit-rate per
# stream, e.g. 2M; default unlimited).
set -euo pipefail
umask 077

usage="usage: fetch-bundle.sh <run-id> <curl-config> [streams]"
run="${1:?$usage}"
config="${2:?$usage}"
streams="${3:-4}"
db="${CRV_DATABASE:-ingestion-catalog}"
base="https://${CRV_BLOB_ACCOUNT:-stawmc6icu2kueq}.blob.core.windows.net/${CRV_BLOB_CONTAINER:-crv-migration}/$run/$db"
secure="${CRV_SECURE:-/srv/crv-secure}"
incoming="$secure/bundles/.incoming-$run"
final="$secure/bundles/$run"
[[ "$run" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "unexpected run id $run" >&2; exit 1; }
[[ "$streams" =~ ^[1-9][0-9]*$ ]] || { echo "streams must be a positive number" >&2; exit 1; }
rate=()
[ -z "${CRV_FETCH_LIMIT_RATE:-}" ] || rate=(--limit-rate "$CRV_FETCH_LIMIT_RATE")
chunk="${CRV_FETCH_CHUNK:-1048576}"
[[ "$chunk" =~ ^[1-9][0-9]*$ ]] || { echo "CRV_FETCH_CHUNK must be a byte count" >&2; exit 1; }

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
boot=$(($(date +%s) - $(cut -d. -f1 /proc/uptime)))
cleanup() {
    local rc=$?
    jobs -p | xargs -r kill 2> /dev/null || true
    [ "$rc" -eq 0 ] || echo "$(stamp) FETCH-FAILED (exit $rc)"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
# Each request re-reads the config, so a refreshed token reaches the next attempt.
request() {
    curl -4 -sS --fail --connect-timeout 20 --speed-limit 10240 --speed-time 60 "${rate[@]}" \
        -K "$config" "$@" < /dev/null
}
# Appends only the bytes of a 206 reply: they continue the file exactly where it stopped. Small
# requests keep little data queued in a TCP-based VPN, which otherwise stalls under bulk load.
fetch_range() {
    local url="$1" start="$2" end="$3" file="$4" want have last code failures=0
    want=$((end - start + 1))
    touch "$file"
    while :; do
        have=$(stat -c %s "$file")
        [ "$have" -lt "$want" ] || break
        last=$((start + have + chunk - 1))
        [ "$last" -le "$end" ] || last="$end"
        code="$(request --max-time 180 -r "$((start + have))-$last" -o "$file.tmp" \
            -w '%{http_code}' "$url")" || true
        if [ "$code" = 206 ] && [ -f "$file.tmp" ]; then
            cat "$file.tmp" >> "$file"
        fi
        rm -f "$file.tmp"
        if [ "$(stat -c %s "$file")" -gt "$have" ]; then
            failures=0
        else
            failures=$((failures + 1))
            [ "$failures" -lt 60 ] || break
            sleep 10
        fi
    done
    [ "$(stat -c %s "$file")" -eq "$want" ] || { echo "$(stamp) $file incomplete" >&2; return 1; }
}

mountpoint -q "$secure" || { echo "$secure is not mounted" >&2; exit 1; }
[ ! -e "$final" ] || { echo "$final already exists" >&2; exit 1; }
mkdir -p "$incoming/$db"
cd "$incoming/$db"
started=$(date +%s)
request -o SHA256SUMS.new "$base/SHA256SUMS"
if [ -f SHA256SUMS ] && ! cmp -s SHA256SUMS SHA256SUMS.new; then
    echo "SHA256SUMS changed since the partial download in $incoming" >&2
    exit 1
fi
mv SHA256SUMS.new SHA256SUMS
total=0
while read -r _ name; do
    name="${name#\*}"
    [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "unexpected file name $name" >&2; exit 1; }
    size="$(request -I "$base/$name" | tr -d '\r' | awk 'tolower($1) == "content-length:" {print $2}')"
    [[ "$size" =~ ^[0-9]+$ ]] || { echo "no size for $name" >&2; exit 1; }
    total=$((total + size))
    if [ -f "$name" ] && [ "$(stat -c %s "$name")" -eq "$size" ]; then
        if awk -v n="$name" '{f = $2; sub(/^\*/, "", f)} f == n' SHA256SUMS \
                | sha256sum --check --strict --quiet > /dev/null 2>&1; then
            echo "$(stamp) $name already complete"
            continue
        fi
        echo "$(stamp) $name has the right size but not the right checksum: fetching it again"
    fi
    rm -f "$name"
    t0=$(date +%s)
    n=1
    [ "$size" -lt $((64 * 1024 * 1024)) ] || n="$streams"
    part=$(((size + n - 1) / n))
    # Ranges from an earlier run are reused only when they were cut the same way.
    if [ "$(cat "$name.layout" 2> /dev/null)" != "$size $part" ]; then
        rm -f "$name".part*
        echo "$size $part" > "$name.layout"
    fi
    rm -f "$name".part*.tmp
    for p in "$name".part[0-9][0-9][0-9]; do
        [ -f "$p" ] && [ "$(stat -c %Y "$p")" -lt "$boot" ] || continue
        keep=$(($(stat -c %s "$p") - 64 * 1024 * 1024))
        [ "$keep" -gt 0 ] || keep=0
        truncate -s "$keep" "$p"
        echo "$(stamp) $p was written before the last restart: resuming it from $keep bytes"
    done
    pids=()
    parts=()
    for ((i = 0; i * part < size; i++)); do
        start=$((i * part))
        end=$((start + part - 1))
        [ "$end" -lt "$size" ] || end=$((size - 1))
        parts+=("$(printf '%s.part%03d' "$name" "$i")")
        fetch_range "$base/$name" "$start" "$end" "${parts[-1]}" &
        pids+=("$!")
    done
    failed=0
    for pid in "${pids[@]}"; do wait "$pid" || failed=1; done
    [ "$failed" -eq 0 ] || { echo "a range of $name failed" >&2; exit 1; }
    cat "${parts[@]}" > "$name"
    rm -f "${parts[@]}" "$name.layout"
    [ "$(stat -c %s "$name")" -eq "$size" ] || { echo "$name has the wrong size" >&2; exit 1; }
    echo "$(stamp) $name $size bytes in $(($(date +%s) - t0))s"
done < SHA256SUMS
sha256sum --check --strict SHA256SUMS
mv "$incoming" "$final"
secs=$(($(date +%s) - started))
[ "$secs" -gt 0 ] || secs=1
echo "$(stamp) FETCH-DONE $total bytes in ${secs}s -> $final"
