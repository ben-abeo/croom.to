#!/bin/bash
#
# Nightly dump of the Crystal Meet dashboard database.
# Usage: backup.sh DEPLOY_DIR BACKUP_DIR [KEEP_DAYS]
#   DEPLOY_DIR holds docker-compose.yml and .env; BACKUP_DIR receives
#   croom-dashboard-YYYY-MM-DD.sql.gz (mode 600); older files are deleted.
#
set -euo pipefail

DEPLOY_DIR="${1:?usage: backup.sh DEPLOY_DIR BACKUP_DIR [KEEP_DAYS]}"
BACKUP_DIR="${2:?usage: backup.sh DEPLOY_DIR BACKUP_DIR [KEEP_DAYS]}"
KEEP_DAYS="${3:-14}"

set -a
# shellcheck disable=SC1091
. "$DEPLOY_DIR/.env"
set +a

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
umask 077

OUT="$BACKUP_DIR/croom-dashboard-$(date +%F).sql.gz"
PART="$OUT.part"
trap 'rm -f "$PART"' EXIT

docker compose -f "$DEPLOY_DIR/docker-compose.yml" exec -T db \
    pg_dump --clean --if-exists -U "${DB_USER:-croom}" "${DB_NAME:-croom}" | gzip > "$PART"
mv "$PART" "$OUT"

find "$BACKUP_DIR" -name 'croom-dashboard-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "wrote $OUT"
