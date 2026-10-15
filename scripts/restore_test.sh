#!/usr/bin/env bash
#
# Prove a backup restores. Run on the host, from the repository directory:
#
#   scripts/restore_test.sh                    # the newest dump in $BACKUP_DIR
#   scripts/restore_test.sh backups/X.dump     # a particular one
#
# Restores the dump into a scratch database next to the live one, then compares
# the two: the migration revision, and the row count of every table. Exits
# non-zero on any difference, and always drops the scratch database.
#
# Counts are compared against the live database as it is now, so on a busy
# server rows written since the dump show as differences. Run it in a quiet hour,
# or allow for that with RESTORE_ROW_TOLERANCE (rows per table).
set -euo pipefail

: "${COMPOSE_FILE:=docker-compose.prod.yml}"
: "${ENV_FILE:=.env.production}"
: "${BACKUP_DIR:=./backups}"
: "${RESTORE_ROW_TOLERANCE:=0}"
export COMPOSE_FILE

compose() {
    if [ -f "${ENV_FILE}" ]; then
        docker compose --env-file "${ENV_FILE}" "$@"
    else
        docker compose "$@"
    fi
}

dump="${1:-$(ls -1t "${BACKUP_DIR}"/myradius-*.dump 2>/dev/null | head -n 1 || true)}"
if [ -z "${dump}" ] || [ ! -f "${dump}" ]; then
    echo "restore-test: no dump found" >&2
    exit 1
fi
scratch="restore_test_$(date -u +%Y%m%d%H%M%S)"

psql_in() {
    # psql_in DATABASE SQL — tab-separated, no headers.
    compose exec -T postgres sh -c "psql -U \"\$POSTGRES_USER\" -d \"$1\" -At -F '	' -v ON_ERROR_STOP=1 -c \"\$0\"" "$2"
}

live_db="$(compose exec -T postgres sh -c 'echo "$POSTGRES_DB"' | tr -d '\r')"

cleanup() {
    psql_in "${live_db}" "DROP DATABASE IF EXISTS ${scratch}" >/dev/null || true
}
trap cleanup EXIT

echo "restore-test: restoring ${dump} into ${scratch}"
psql_in "${live_db}" "CREATE DATABASE ${scratch}" >/dev/null
compose exec -T postgres sh -c "pg_restore -U \"\$POSTGRES_USER\" -d ${scratch} --no-owner --exit-on-error" < "${dump}"

counts_sql="SELECT table_name, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM public.%I', table_name), false, true, '')))[1]::text FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"

live="$(psql_in "${live_db}" "${counts_sql}" | tr -d '\r')"
restored="$(psql_in "${scratch}" "${counts_sql}" | tr -d '\r')"
live_rev="$(psql_in "${live_db}" "SELECT version_num FROM alembic_version" | tr -d '\r')"
restored_rev="$(psql_in "${scratch}" "SELECT version_num FROM alembic_version" | tr -d '\r')"

status=0
if [ "${live_rev}" != "${restored_rev}" ]; then
    echo "restore-test: revision differs: live ${live_rev}, restored ${restored_rev}"
    status=1
fi

tables=0
while IFS='	' read -r table live_count; do
    [ -z "${table}" ] && continue
    tables=$((tables + 1))
    restored_count="$(printf '%s\n' "${restored}" | awk -F '	' -v t="${table}" '$1 == t { print $2 }')"
    if [ -z "${restored_count}" ]; then
        echo "restore-test: ${table} is missing from the restore"
        status=1
        continue
    fi
    difference=$((live_count - restored_count))
    [ "${difference}" -lt 0 ] && difference=$((-difference))
    if [ "${difference}" -gt "${RESTORE_ROW_TOLERANCE}" ]; then
        echo "restore-test: ${table}: live ${live_count}, restored ${restored_count}"
        status=1
    fi
done <<< "${live}"

total="$(printf '%s\n' "${restored}" | awk -F '	' '{ sum += $2 } END { print sum + 0 }')"
if [ "${status}" -eq 0 ]; then
    echo "restore-test: OK — revision ${restored_rev}, ${tables} tables, ${total} rows match"
else
    echo "restore-test: FAILED" >&2
fi
exit "${status}"
