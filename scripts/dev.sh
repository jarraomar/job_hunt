#!/usr/bin/env bash
# Run the whole thing locally: database up, migrations applied, UI on :8000.
#
#   ./scripts/dev.sh              serve the UI at http://localhost:8000
#   ./scripts/dev.sh discover     one discovery pass, then exit
#   ./scripts/dev.sh score        one scoring pass, then exit
#
# Reads real credentials from the environment when they are present and works
# without them: discovery needs no API key, and the UI only needs the database.
set -euo pipefail

cd "$(dirname "$0")/.."

export DATABASE_URL="${DATABASE_URL:-postgresql://jobhunt@localhost:5433/jobhunt_dev}"
export JOBHUNT_PROFILE_DIR="${JOBHUNT_PROFILE_DIR:-$HOME/.local/share/jobhunt/profile}"

PY=./venv/bin/python

# The dev cluster lives outside the repo; `up` is idempotent.
./scripts/dev_db.sh up >/dev/null

# createdb is not idempotent, so the failure case is the normal one.
PGBIN="/opt/homebrew/opt/postgresql@16/bin"
"$PGBIN/createdb" -h localhost -p 5433 -U jobhunt jobhunt_dev 2>/dev/null || true
"$PY" scripts/migrate.py

case "${1:-serve}" in
  discover)
    "$PY" -c "
import asyncio, logging
from pipeline.config import load_settings
from pipeline.db import connection
from pipeline.http import PoliteSession
from pipeline.run_discover import run
from pipeline.sources.base import SourceConfig
from pipeline.sources.registry import SOURCES, load_targets
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
async def main():
    s = load_settings()
    targets = load_targets(s.profile_dir / 'targets.yaml')
    async with connection() as conn, PoliteSession(s.user_agent, conn=conn) as sess:
        print(await run(conn, SourceConfig(session=sess, targets=targets, settings=s), SOURCES))
asyncio.run(main())"
    ;;

  score)
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
      echo "note: ANTHROPIC_API_KEY unset — embedding and ranking will run," >&2
      echo "      judging and the Sharia screen will not." >&2
    fi
    "$PY" -c "
import asyncio, logging
from pipeline.config import load_settings
from pipeline.db import connection
from pipeline.run_score import run
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
async def main():
    async with connection() as conn:
        print(await run(conn, load_settings()))
asyncio.run(main())"
    ;;

  serve)
    ./scripts/build_css.sh
    echo
    echo "  queue     http://localhost:8000/"
    echo "  tracker   http://localhost:8000/tracker"
    echo "  settings  http://localhost:8000/settings"
    echo
    # --reload watches web/ and pipeline/ so template edits show on refresh.
    exec ./venv/bin/uvicorn api.index:app --reload \
         --reload-dir web --reload-dir pipeline --reload-dir api \
         --port 8000
    ;;

  *)
    echo "usage: $0 {serve|discover|score}" >&2
    exit 1
    ;;
esac
