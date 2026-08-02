#!/bin/sh
set -eu

: "${POSTGRES_HOST:?POSTGRES_HOST is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${BACKUP_ENCRYPTION_KEY:?BACKUP_ENCRYPTION_KEY is required}"

export PGPASSWORD="$POSTGRES_PASSWORD"
mkdir -p /backups/daily /backups/weekly

cleanup() {
  rm -f /tmp/vpn-backup.dump /tmp/vpn-backup.verify /backups/daily/.pending.enc
}
trap cleanup EXIT INT TERM

until pg_isready -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do
  echo "database is not ready; retrying"
  sleep 5
done

rotate_files() {
  directory="$1"
  keep="$2"
  count=0
  for file in $(ls -1t "$directory"/*.dump.enc 2>/dev/null || true); do
    count=$((count + 1))
    if [ "$count" -gt "$keep" ]; then
      rm -f "$file"
    fi
  done
}

backup_once() {
  cleanup
  day=$(date -u +%F)
  daily_file="/backups/daily/vpn-${day}.dump.enc"
  echo "creating encrypted PostgreSQL backup for ${day}"
  pg_dump -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --format=custom --no-owner --no-acl --file=/tmp/vpn-backup.dump
  openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 \
    -in /tmp/vpn-backup.dump -out /backups/daily/.pending.enc \
    -pass env:BACKUP_ENCRYPTION_KEY
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -in /backups/daily/.pending.enc -out /tmp/vpn-backup.verify \
    -pass env:BACKUP_ENCRYPTION_KEY
  pg_restore --list /tmp/vpn-backup.verify >/dev/null
  mv /backups/daily/.pending.enc "$daily_file"
  if [ "$(date -u +%u)" = "7" ]; then
    cp "$daily_file" "/backups/weekly/vpn-$(date -u +%G-W%V).dump.enc"
  fi
  rotate_files /backups/daily 7
  rotate_files /backups/weekly 4
  echo "backup verified: $(basename "$daily_file")"
  cleanup
}

backup_once
if [ "${BACKUP_RUN_ONCE:-0}" = "1" ]; then
  exit 0
fi
while true; do
  sleep "${BACKUP_INTERVAL_SECONDS:-86400}"
  backup_once
done
