"""Apply pending SQL migrations.

Run by CI before a deploy is promoted, never at request time. A serverless
function can cold-start concurrently, so migrating on startup means racing
migrations (spec section 5).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from psycopg import AsyncConnection
from psycopg.rows import scalar_row

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
  name       TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def apply_migrations(conn: AsyncConnection, migrations_dir: Path) -> list[str]:
    """Apply every *.sql not yet recorded, in filename order.

    Returns the names applied by this call, so a caller can tell "already up to
    date" from "just migrated". Requires an autocommit connection: the explicit
    transaction per migration controls its own boundaries.
    """
    await conn.execute(_SCHEMA_VERSION_DDL)
    # Pin the row factory rather than inheriting the caller's: this is called
    # both with tuple-row connections (the CLI) and dict-row ones (the app).
    async with conn.cursor(row_factory=scalar_row) as cur:
        await cur.execute("SELECT name FROM schema_version")
        done = set(await cur.fetchall())

    applied: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in done:
            continue
        # Postgres DDL is transactional, so a migration that fails partway
        # leaves no half-built schema behind.
        async with conn.transaction():
            await conn.execute(path.read_text())
            await conn.execute("INSERT INTO schema_version (name) VALUES (%s)", (path.name,))
        applied.append(path.name)
    return applied


async def _main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    conn = await AsyncConnection.connect(dsn, autocommit=True)
    try:
        applied = await apply_migrations(conn, migrations)
    finally:
        await conn.close()

    print(f"applied: {', '.join(applied)}" if applied else "already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
