#!/usr/bin/env bash
# Encrypted (LUKS2) file-backed volume on dgx-niz01 for the CRV staging database, export bundles,
# restore evidence and the staging password. Run with sudo from the operator account. The
# passphrase is typed on this terminal and never stored: open the volume by hand after a reboot,
# then start the staging server.
#   sudo bash luks-volume.sh create [size]   allocate, format and mount (once; default 100G)
#   sudo bash luks-volume.sh open            unlock and mount
#   sudo bash luks-volume.sh close           unmount and lock (stop the staging server first)
#   sudo bash luks-volume.sh status
set -euo pipefail
umask 077

image="${CRV_SECURE_IMAGE:-/var/lib/crv-secure/volume.luks}"
name=crv-secure
mapper="/dev/mapper/$name"
mount_point="${CRV_SECURE_MOUNT:-/srv/crv-secure}"

die() {
    echo "luks-volume.sh: $*" >&2
    exit 1
}
[ "$(id -u)" -eq 0 ] || die "run with sudo"
operator="${SUDO_USER:-}"
[ -n "$operator" ] && [ "$operator" != root ] || die "run with sudo from the operator account"

mount_volume() {
    if mountpoint -q "$mount_point"; then
        return
    fi
    # The empty mount point is immutable, so nothing lands on the plain disk while it is locked.
    [ -d "$mount_point" ] || mkdir -m 0755 "$mount_point"
    chattr +i "$mount_point"
    mount -o nodev,nosuid,noexec "$mapper" "$mount_point"
    chmod 0711 "$mount_point"
}

create() {
    local size="${1:-100G}" passphrase confirm
    [[ "$size" =~ ^[1-9][0-9]*[GT]$ ]] || die "size must look like 100G"
    [ ! -e "$image" ] || die "$image already exists"
    [ ! -e "$mapper" ] || die "$mapper is already open"
    read -rsp "New LUKS passphrase: " passphrase
    echo >&2
    read -rsp "Repeat the passphrase: " confirm
    echo >&2
    [ "$passphrase" = "$confirm" ] || die "the passphrases differ"
    [ "${#passphrase}" -ge 12 ] || die "use at least 12 characters"
    install -d -m 0700 "$(dirname "$image")"
    fallocate -l "$size" "$image"
    chmod 0600 "$image"
    # printf is a builtin: the passphrase never appears in a process argument list.
    printf '%s' "$passphrase" | cryptsetup luksFormat --batch-mode --type luks2 \
        --pbkdf argon2id --key-file=- "$image"
    printf '%s' "$passphrase" | cryptsetup open --key-file=- "$image" "$name"
    unset passphrase confirm
    mkfs.ext4 -q -L "$name" "$mapper"
    mount_volume
    # The PostgreSQL image takes ownership of pgdata itself when it starts.
    install -d -m 0700 "$mount_point/pgdata"
    install -d -m 0700 -o "$operator" -g "$operator" \
        "$mount_point/bundles" "$mount_point/restore-logs" "$mount_point/secrets" \
        "$mount_point/sources"
    head -c 32 /dev/urandom | base64 > "$mount_point/secrets/pg16-staging.password"
    chown "$operator:$operator" "$mount_point/secrets/pg16-staging.password"
    chmod 0600 "$mount_point/secrets/pg16-staging.password"
    echo "created $image ($size), mounted at $mount_point"
}

open_volume() {
    [ -e "$image" ] || die "$image not found; run create first"
    [ -e "$mapper" ] || cryptsetup open "$image" "$name"
    mount_volume
    echo "mounted at $mount_point"
}

close_volume() {
    local sources
    sources="$(docker ps -q | xargs -r docker inspect --format '{{range .Mounts}}{{.Source}} {{end}}')"
    if [[ " $sources" == *" $mount_point"* ]]; then
        die "a running container uses $mount_point; stop the staging server first"
    fi
    if mountpoint -q "$mount_point"; then
        umount "$mount_point"
    fi
    if [ -e "$mapper" ]; then
        cryptsetup close "$name"
    fi
    echo "locked"
}

case "${1:-}" in
    create) create "${2:-}" ;;
    open) open_volume ;;
    close) close_volume ;;
    status)
        cryptsetup status "$name" || true
        findmnt "$mount_point" || true
        ;;
    *) die "usage: luks-volume.sh create [size] | open | close | status" ;;
esac
