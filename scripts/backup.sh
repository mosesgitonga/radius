#!/usr/bin/env bash
#
# Nightly database backup. Run on the host, from the repository directory:
#
#   scripts/backup.sh
#
# 1. pg_dump, custom format, from inside the postgres container, to $BACKUP_DIR;
# 2. checks the dump reads back (pg_restore --list), so a truncated file fails now
#    rather than on the day it is needed;
# 3. deletes local dumps older than $BACKUP_RETENTION_DAYS;
# 4. if $BACKUP_BUCKET is set, copies the dump off the machine to S3-compatible
#    storage (Cloudflare R2) and prunes the copies there to the same retention.
#
# Schedule it from the host's crontab, e.g. 01:15 UTC (04:15 in Nairobi):
#   15 1 * * * cd /srv/myradius && scripts/backup.sh >> /var/log/myradius-backup.log 2>&1
#
# Dumps hold personal data. The directory is created mode 700, files mode 600,
# and the off-site bucket must be private.
set -euo pipefail

: "${COMPOSE_FILE:=docker-compose.prod.yml}"
: "${ENV_FILE:=.env.production}"
: "${BACKUP_DIR:=./backups}"
: "${BACKUP_RETENTION_DAYS:=14}"
export COMPOSE_FILE

compose() {
    if [ -f "${ENV_FILE}" ]; then
        docker compose --env-file "${ENV_FILE}" "$@"
    else
        docker compose "$@"
    fi
}

umask 077
mkdir -p "${BACKUP_DIR}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="myradius-${stamp}.dump"
path="${BACKUP_DIR}/${name}"

echo "backup: dumping to ${path}"
compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6' > "${path}.partial"
mv "${path}.partial" "${path}"

echo "backup: verifying"
entries="$(compose exec -T postgres pg_restore --list < "${path}" | grep -c 'TABLE DATA' || true)"
if [ "${entries}" -lt 1 ]; then
    echo "backup: FAILED: ${path} has no table data" >&2
    exit 1
fi
echo "backup: ${entries} tables, $(du -h "${path}" | cut -f1)"

find "${BACKUP_DIR}" -name 'myradius-*.dump' -type f -mtime "+${BACKUP_RETENTION_DAYS}" -print -delete

# The bucket is configured in the env file the containers read; the host shell
# does not load it, so look there when it is not exported here.
if [ -z "${BACKUP_BUCKET:-}" ] && [ -f "${ENV_FILE}" ]; then
    BACKUP_BUCKET="$(sed -n 's/^BACKUP_BUCKET=//p' "${ENV_FILE}" | tail -n 1)"
fi
if [ -n "${BACKUP_BUCKET:-}" ]; then
    echo "backup: copying off-site"
    compose exec -T api python -m scripts.backup_offsite put "${name}" < "${path}"
    compose exec -T api python -m scripts.backup_offsite prune --days "${BACKUP_RETENTION_DAYS}"
fi
echo "backup: done"
