#!/usr/bin/env bash
# Local Postgres for the test suite.
#
# Tests run against a real engine, not a stub: FOR UPDATE SKIP LOCKED, partial
# unique indexes, and ON CONFLICT have no equivalent in a fake (spec section 17).
#
# The cluster lives in .pgdata/ (gitignored) on port 5433, so it cannot collide
# with any other Postgres on this machine. CI uses a postgres:16 service
# container instead and never runs this script.
#
#   ./scripts/dev_db.sh up      create if needed, then start
#   ./scripts/dev_db.sh down    stop
#   ./scripts/dev_db.sh reset      drop and recreate the TEST database only
#   ./scripts/dev_db.sh nuke       destroy the whole cluster (loses jobhunt_dev too)
#   ./scripts/dev_db.sh status
set -euo pipefail

# Deliberately outside the repo. A cluster inside the project tree gets walked
# by `vercel deploy` (its socket lock file caused an ENOENT mid-upload), by
# ruff, and by anything else that globs the working directory.
PGDATA="${JOBHUNT_PGDATA:-$HOME/.local/share/jobhunt/pgdata}"
PORT=5433
PGBIN="/opt/homebrew/opt/postgresql@16/bin"
DB=jobhunt_test
USER=jobhunt

[ -x "$PGBIN/postgres" ] || { echo "postgresql@16 not found at $PGBIN" >&2; exit 1; }

start() {
  mkdir -p "$(dirname "$PGDATA")"
  if [ ! -d "$PGDATA" ]; then
    echo "initializing cluster in $PGDATA"
    "$PGBIN/initdb" -D "$PGDATA" -U "$USER" --auth=trust >/dev/null
  fi
  if "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
    echo "already running on port $PORT"
  else
    "$PGBIN/pg_ctl" -D "$PGDATA" -o "-p $PORT -k $PGDATA" -l "$PGDATA/server.log" start
  fi
  # createdb is not idempotent; ignore the "already exists" case.
  "$PGBIN/createdb" -h localhost -p "$PORT" -U "$USER" "$DB" 2>/dev/null || true
  echo "ready: postgresql://$USER@localhost:$PORT/$DB"
}

case "${1:-up}" in
  up)     start ;;
  down)   "$PGBIN/pg_ctl" -D "$PGDATA" stop || true ;;
  # Drops only the test database. `nuke` used to be spelled `reset`, which
  # twice destroyed the dev corpus as collateral damage while verifying a
  # migration test actually fails without its migration.
  reset)  start >/dev/null
          "$PGBIN/dropdb" -h localhost -p "$PORT" -U "$USER" --if-exists "$DB"
          "$PGBIN/createdb" -h localhost -p "$PORT" -U "$USER" "$DB"
          echo "recreated $DB (jobhunt_dev untouched)" ;;
  nuke)   "$PGBIN/pg_ctl" -D "$PGDATA" stop 2>/dev/null || true; rm -rf "$PGDATA"; start ;;
  status) "$PGBIN/pg_ctl" -D "$PGDATA" status ;;
  *)      echo "usage: $0 {up|down|reset|nuke|status}" >&2; exit 1 ;;
esac
