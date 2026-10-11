#!/bin/bash
#
# Creates the database the test suite uses, alongside the development one.
#
# Runs once, during initdb, on an empty data directory. If you already have a
# postgres-data volume, either recreate it (`docker compose down -v`) or create
# the database by hand:
#
#   docker compose exec postgres createdb -U myradius myradius_test
#
# The extensions themselves are not created here: migration 0001 does that, so
# the schema is reproducible on any PostgreSQL 16 rather than only on this image.
set -euo pipefail

TEST_DB="${POSTGRES_DB}_test"

psql --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-SQL
    CREATE DATABASE ${TEST_DB} OWNER ${POSTGRES_USER};
SQL

echo "init: created database ${TEST_DB}"
