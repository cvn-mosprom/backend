#!/usr/bin/env sh
# Restore a dump made by scripts/backup.sh into the compose database (replaces its contents!).
#
#   sh scripts/restore.sh backups/mosprom_20260101T000000Z.dump [--yes]
#
# What it does: takes a safety dump of the current database (backups/pre_restore_*.dump), stops api + bot, runs
# `pg_restore --clean --if-exists --single-transaction`, runs the migrations to head (an older dump is brought up to date),
# starts api + bot again. RESTORE_DB=<name> restores into another, already existing database instead (api/bot stay up,
# no safety dump) - handy to check a backup: CREATE DATABASE restore_check; RESTORE_DB=restore_check sh scripts/restore.sh <file> --yes
set -eu
cd "$(dirname "$0")/.."

FILE=${1:-}
[ -n "$FILE" ] && [ -f "$FILE" ] || { echo "usage: sh scripts/restore.sh <backups/file.dump> [--yes]" >&2; exit 2; }
TARGET=${RESTORE_DB:-}

if [ "${2:-}" != "--yes" ]; then
    printf 'This REPLACES the %s database with %s. Type "restore" to continue: ' "${TARGET:-compose}" "$FILE"
    read -r answer
    [ "$answer" = "restore" ] || { echo "aborted"; exit 1; }
fi

if [ -z "$TARGET" ]; then
    echo "safety dump of the current database ..."
    BACKUP_PREFIX=pre_restore sh scripts/backup.sh
    docker compose stop api bot
fi

docker compose exec -T -e RESTORE_DB="$TARGET" db sh -c \
    'pg_restore -U "$POSTGRES_USER" -d "${RESTORE_DB:-$POSTGRES_DB}" --clean --if-exists --no-owner --single-transaction' < "$FILE"

if [ -z "$TARGET" ]; then
    docker compose run --rm migrate
    docker compose start api bot
fi
echo "restored $FILE into ${TARGET:-the compose database}"
