"""Dry-run the blocklist over every known company. Costs nothing, writes nothing.

Read every EXCLUDED result. A false exclusion silently deletes an employer and
nobody finds out -- that is the expensive direction of error in this screen,
and the allowed_overrides list in blocklist.yaml exists because of it.
"""

from __future__ import annotations

import asyncio

from psycopg.rows import dict_row

from pipeline.db import connection
from pipeline.filters.sharia import screen_blocklist

# `companies` has no description column -- the only self-description held is
# the text of that company's job postings. One representative posting is enough
# for a business-activity screen.
SQL = """
SELECT DISTINCT ON (c.company_id)
       c.company_id, c.name, COALESCE(j.description, '') AS description
FROM companies c
LEFT JOIN jobs j USING (company_id)
ORDER BY c.company_id, j.last_seen_at DESC
"""


async def main() -> None:
    async with connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(SQL)
        rows = await cur.fetchall()

    excluded, unresolved = [], []
    for row in rows:
        if result := screen_blocklist(row["name"], row["description"]):
            excluded.append((row["name"], result[1], result[2]))
        else:
            unresolved.append(row["name"])

    print(
        f"{len(rows)} companies: {len(excluded)} excluded by blocklist, "
        f"{len(unresolved)} would go to Haiku "
        f"(~${len(unresolved) * 0.0005:.2f} once, then cached forever)\n"
    )
    print("EXCLUDED — read every one of these:")
    for name, sector, reason in excluded:
        print(f"  {name:34s} [{sector}] {reason}")


if __name__ == "__main__":
    asyncio.run(main())
