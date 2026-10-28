#!/usr/bin/env bash
# Archive a Redis-consistent RDB image alongside the PostgreSQL backup.
set -euo pipefail
umask 077
backup_dir=/var/backups/multi-agents
mkdir -p "$backup_dir"
stamp=$(date -u +%Y%m%d_%H%M%S)
tmp="$backup_dir/.redis_${stamp}.rdb.partial"
final="$backup_dir/redis_${stamp}.rdb"
container_tmp="/tmp/redis_backup_${stamp}_$$.rdb"
cleanup() {
  rm -f "$tmp"
  docker exec multi-agents-redis-1 rm -f "$container_tmp" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# redis-cli --rdb streams an internally consistent point-in-time snapshot.
docker exec multi-agents-redis-1 redis-cli --rdb "$container_tmp" >/dev/null
docker cp "multi-agents-redis-1:$container_tmp" "$tmp"
test -s "$tmp"
[[ "$(head -c 5 "$tmp")" == 'REDIS' ]]
mv "$tmp" "$final"
find "$backup_dir" -maxdepth 1 -type f -name 'redis_*.rdb' -mtime +6 -delete
printf 'REDIS_BACKUP_CREATED=%s\n' "$final"
