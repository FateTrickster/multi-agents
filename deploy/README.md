# Production deployment

Use an isolated Docker Compose stack.

## Redis persistence and writing snapshot retention

The production `redis` service runs with AOF enabled and a persistent `redis_data` volume. Shared-writing five-minute snapshots live in Redis hashes (key format `room:{room_id}:writing_doc_snapshots_5min`), without an application expiry. They must be included in backups alongside PostgreSQL.

On the server, install `deploy/backup-db.sh` as `/usr/local/sbin/multi-agents-backup-db` and `deploy/backup-redis.sh` as `/usr/local/sbin/multi-agents-backup-redis`, with executable permissions; install `deploy/multi-agents-backup.cron` under `/etc/cron.d/`. Configure backup storage permissions to restrict access to research data. The schedule backs up PostgreSQL at 03:10 and Redis at 03:20 (server time). Both scripts keep 7 days of local backups in `/var/backups/multi-agents`.

To verify a Redis backup, run `redis-check-rdb` against the exported `.rdb` file, then restore it into an **isolated Redis instance**, not the production one, and verify that example writing snapshot hashes and document states can be read. Validate disaster recovery and off-host backup retention before using real participant data.

The test Compose file (`docker-compose.yml`) does not enable Redis persistence. It is for local development only, not long-running research data collection.
