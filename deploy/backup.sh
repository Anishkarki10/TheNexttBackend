#!/bin/sh
# Nightly Postgres dump, keeps the last 14. Run from the repo directory, e.g. crontab:
#   0 3 * * * cd /opt/TheNexttBackend && ./deploy/backup.sh >> /var/log/thenext-backup.log 2>&1
set -e
BACKUP_DIR="${BACKUP_DIR:-/var/backups/thenext}"
mkdir -p "$BACKUP_DIR"
. ./.env
FILE="$BACKUP_DIR/db-$(date +%Y%m%d-%H%M%S).sql.gz"
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$FILE"
docker compose cp api:/app/uploads - | gzip > "$BACKUP_DIR/uploads-$(date +%Y%m%d).tar.gz"
ls -1t "$BACKUP_DIR"/db-*.sql.gz | tail -n +15 | xargs -r rm --
ls -1t "$BACKUP_DIR"/uploads-*.tar.gz | tail -n +15 | xargs -r rm --
echo "Backed up to $FILE"
