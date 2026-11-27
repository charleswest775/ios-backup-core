"""Health: the two empty databases every encrypted backup holds.

No pack has Health data yet, so "health" is still a planned data type (generate.PLANNED), always
"absent". But a real encrypted backup always has HealthDomain's healthdb.sqlite and
healthdb_secure.sqlite (Apple leaves them out of unencrypted backups), so readers meet them
there. Each is built from the snapshot's harvested schema with no rows. This draws no random
numbers. A writer for Health data, with its STEP, replaces this.
"""

from __future__ import annotations

from pathlib import Path

from backupgen.sqlitegen import build

DOMAIN = "HealthDomain"
DATABASES = ("healthdb.sqlite", "healthdb_secure.sqlite")


def write_empty(writer, schema: str, tmp: Path) -> None:
    """Add both databases, empty, to an encrypted backup; nothing to an unencrypted one."""
    if not writer.encrypted:
        return
    for database in DATABASES:
        writer.add_file(DOMAIN, f"Health/{database}", build(tmp / database, schema, database, lambda db: None))
