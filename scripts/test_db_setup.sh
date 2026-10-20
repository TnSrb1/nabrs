#!/usr/bin/env bash
# The common PG preparation for tests/db — ONE source for scripts/gate.sh and ci.yml.
# Usage (source, so that the variables get into the environment of the caller):
#   . scripts/test_db_setup.sh <host> <port> [db]
# PG (pgvector/pgvector:pg16, superuser postgres/password t) must already be listening.
# Checks one alembic head, runs alembic upgrade head (the migrations create
# the role tendersrb_app with the password APP_DB_PASSWORD) and exports
# TEST_SUPERUSER_DATABASE_URL / TEST_RLS_DATABASE_URL / APP_DB_PASSWORD.
# Return 0 = ready; otherwise 1 (the reason on stdout).
_tdb_host="${1:?host}"; _tdb_port="${2:?port}"; _tdb_name="${3:-tendersrb}"
_tdb_su="postgresql+asyncpg://postgres:t@${_tdb_host}:${_tdb_port}/${_tdb_name}"
_tdb_log=$(mktemp)
_tdb_heads=$(python3 -m alembic heads 2>/dev/null | grep -c "(head)" || true)
if [ "$_tdb_heads" != 1 ]; then
    echo "❌ alembic heads = ${_tdb_heads} (нужна одна голова)"
    rm -f "$_tdb_log"; return 1 2>/dev/null || exit 1
fi
if ! DATABASE_URL="$_tdb_su" APP_DB_PASSWORD=apppass python3 -m alembic upgrade head >"$_tdb_log" 2>&1; then
    echo "❌ alembic upgrade head на чистом PG упал:"; tail -20 "$_tdb_log"
    rm -f "$_tdb_log"; return 1 2>/dev/null || exit 1
fi
rm -f "$_tdb_log"
export TEST_SUPERUSER_DATABASE_URL="$_tdb_su" APP_DB_PASSWORD=apppass
export TEST_RLS_DATABASE_URL="postgresql+asyncpg://tendersrb_app:apppass@${_tdb_host}:${_tdb_port}/${_tdb_name}"
