#!/usr/bin/env bash
# Daily database backup: ~/backups/finance_YYYY-MM-DD.sql.gz, keeps the last 14 days.
# Works with the bundled PostgreSQL and with Neon (DATABASE_URL in .env).
# The setup script runs this every night at 03:00 via cron. Run it by hand any time:
#   bash deploy/oracle/backup.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT="$HOME/backups"; mkdir -p "$OUT"
FILE="$OUT/finance_$(date +%F).sql.gz"
URL=$(grep -E '^DATABASE_URL=' .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | tr -d '"' || true)

if [ -z "$URL" ]; then
  docker compose exec -T db pg_dump -U finance --no-owner finance | gzip > "$FILE"
else
  # Hosted database (Neon): use a throwaway pg_dump container. The bot's "+asyncpg" part is removed.
  URL=${URL/postgresql+asyncpg:/postgresql:}
  docker run --rm postgres:17-alpine pg_dump --no-owner --no-acl "$URL" | gzip > "$FILE"
fi
find "$OUT" -name 'finance_*.sql.gz' -mtime +14 -delete
echo "Backup saved: $FILE ($(du -h "$FILE" | cut -f1))"
