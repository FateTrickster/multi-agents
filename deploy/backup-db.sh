#!/usr/bin/env bash
set -euo pipefail
umask 077
backup_dir=/var/backups/multi-agents
mkdir -p "$backup_dir"
stamp=$(date +%Y%m%d_%H%M%S)
tmp="$backup_dir/.collab_${stamp}.sql.gz.partial"
final="$backup_dir/collab_${stamp}.sql.gz"
trap 'rm -f "$tmp"' EXIT
docker exec multi-agents-postgres-1 pg_dump -U collab -d collab_db | gzip -9 > "$tmp"
test -s "$tmp"
mv "$tmp" "$final"
find "$backup_dir" -maxdepth 1 -type f -name 'collab_*.sql.gz' -mtime +6 -delete
printf 'BACKUP_CREATED=%s\n' "$final"
