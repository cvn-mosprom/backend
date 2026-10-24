#!/usr/bin/env sh
# Logical backup of the mosprom database: pg_dump -Fc into $BACKUP_DIR/mosprom_<UTC timestamp>.dump, newest BACKUP_KEEP kept.
#
#   sh scripts/backup.sh            one dump now (from the repo root; uses `docker compose exec db pg_dump`, the stack must be up)
#   sh scripts/backup.sh --loop     a dump now and then every BACKUP_INTERVAL_H hours (this is what the `backup` compose
#                                   service runs, with PGHOST/PGUSER/PGPASSWORD/PGDATABASE set and pg_dump in the image)
#
# Env: BACKUP_DIR (default ./backups), BACKUP_KEEP (14 by hand, 28 in compose), BACKUP_INTERVAL_H (6),
#      BACKUP_INTERVAL_S (overrides the hours, for tests), BACKUP_PREFIX (mosprom).
# Restore: scripts/restore.sh <file>.
set -eu

PREFIX=${BACKUP_PREFIX:-mosprom}
KEEP=${BACKUP_KEEP:-14}
DIR=${BACKUP_DIR:-}
if [ -z "${PGHOST:-}" ]; then # by hand on the host: relative to the repo root
    cd "$(dirname "$0")/.."
    DIR=${DIR:-./backups}
fi
DIR=${DIR:-/backups}
mkdir -p "$DIR"

log() { printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

dump_once() {
    out="$DIR/${PREFIX}_$(date -u +%Y%m%dT%H%M%SZ).dump"
    tmp="$out.partial"
    rm -f "$tmp"
    if [ -n "${PGHOST:-}" ]; then
        pg_dump -Fc --no-owner "$PGDATABASE" > "$tmp" || { rm -f "$tmp"; return 1; }
        pg_restore --list "$tmp" > /dev/null || { rm -f "$tmp"; return 1; } # readable archive?
    else
        docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc --no-owner "$POSTGRES_DB"' > "$tmp" \
            || { rm -f "$tmp"; return 1; }
        docker compose exec -T db pg_restore --list < "$tmp" > /dev/null || { rm -f "$tmp"; return 1; }
    fi
    [ -s "$tmp" ] || { rm -f "$tmp"; return 1; }
    mv "$tmp" "$out" # appears atomically: never a half-written .dump
    log "wrote $out ($(wc -c < "$out") bytes)"
    n=0
    for f in $(ls -1r "$DIR"/${PREFIX}_*.dump 2> /dev/null); do # timestamped names: reverse sort = newest first
        n=$((n + 1))
        if [ "$n" -gt "$KEEP" ]; then rm -f "$f"; log "rotated out $f"; fi
    done
}

if [ "${1:-}" = "--loop" ]; then
    trap 'exit 0' TERM INT
    wait_s=${BACKUP_INTERVAL_S:-$((${BACKUP_INTERVAL_H:-6} * 3600))}
    log "loop: every ${wait_s}s, keeping $KEEP in $DIR"
    while true; do
        dump_once || log "FAILED (will retry next round)"
        sleep "$wait_s" &
        wait $!
    done
else
    dump_once || { log "FAILED"; exit 1; }
fi
