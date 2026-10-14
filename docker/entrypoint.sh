#!/usr/bin/env bash
#
# Container entrypoint. Dispatches on the first argument:
#
#   api        Gunicorn with Uvicorn workers (production)
#   api-dev    Uvicorn with auto-reload
#   worker     the arq background worker
#   migrate    apply migrations and exit
#   <other>    executed verbatim, so `docker run ... bash` still works
#
set -euo pipefail

# Gunicorn's default of one worker wastes a multi-core VPS; two per core plus one
# is the conventional starting point for I/O-bound ASGI work. Tune against real
# traffic rather than trusting this number.
: "${WEB_CONCURRENCY:=3}"
: "${PORT:=8000}"
: "${RUN_MIGRATIONS:=true}"

run_migrations() {
    echo "entrypoint: applying migrations"
    alembic upgrade head
}

case "${1:-api}" in
api)
    # Migrating on start suits a single-VPS deployment. With more than one
    # replica, set RUN_MIGRATIONS=false and run `migrate` as a separate step, or
    # concurrent containers will race on the same upgrade.
    [ "${RUN_MIGRATIONS}" = "true" ] && run_migrations
    echo "entrypoint: starting api with ${WEB_CONCURRENCY} workers"
    exec gunicorn app.main:app \
        --worker-class uvicorn_worker.UvicornWorker \
        --workers "${WEB_CONCURRENCY}" \
        --bind "0.0.0.0:${PORT}" \
        --timeout 60 \
        --graceful-timeout 30 \
        --keep-alive 5 \
        --max-requests 2000 \
        --max-requests-jitter 200 \
        --access-logfile - \
        --error-logfile -
    ;;
api-dev)
    [ "${RUN_MIGRATIONS}" = "true" ] && run_migrations
    # Development only: create the local bucket if it is missing. The script
    # refuses outside local and test, so this cannot create a production bucket.
    python -m scripts.ensure_bucket || echo "entrypoint: bucket bootstrap failed, continuing"
    echo "entrypoint: starting api with reload"
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --reload
    ;;
worker)
    # The worker never migrates: the API owns the schema, and two processes
    # racing to upgrade is how you get a half-applied migration.
    echo "entrypoint: starting worker"
    exec arq app.workers.settings.WorkerSettings
    ;;
migrate)
    run_migrations
    ;;
*)
    exec "$@"
    ;;
esac
