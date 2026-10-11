"""Build a SQLite database with the exact schema harvested from a real device.

Order matters:
1. Tables and virtual tables.
2. Rows.
3. Indexes, views and triggers.

Apple's triggers call device-only functions (``delete_attachment_path``,
``ab_update_value_from_trigger``). They must exist in the file but must not
fire while we insert.

Some virtual tables use iOS-only tokenizers (Contacts' FTS uses
``ab_cf_tokenizer``) that can't be created here, and SQLite's defensive mode
forbids editing ``sqlite_master``. So the table is created with the built-in
``simple`` tokenizer, padded with spaces to the same byte length. After the file
is closed, the real definition text is swapped in. Equal length means the
file's structure is untouched. The result opens exactly like a real backup's
database, including the FTS table being unusable off-device.

Finally the file is switched to WAL mode and checkpointed, and the -wal/-shm
files are removed. That is what a backup hands over.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Callable, Iterable, Optional

from backupgen import SCHEMAS_DIR


def _placeholder_tokenizer(sql: str) -> str:
    match = re.search(r"tokenize=(\w+)", sql)
    if not match or len(match.group(1)) < len("simple"):
        raise RuntimeError(f"can't create virtual table and no tokenizer to substitute: {sql[:80]}")
    name = match.group(1)
    return sql.replace(f"tokenize={name}", "tokenize=simple" + " " * (len(name) - len("simple")), 1)


def _version(folder: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in folder.name.removeprefix("ios-").split("."))


def load_schema(version: str, database: str) -> dict:
    """The schema harvested for this iOS version or, when there isn't one, the nearest older one.

    The Simulator has no Notes, Phone or Voicemail, so newer versions can lack those databases.
    """
    path = SCHEMAS_DIR / f"ios-{version}" / f"{database}.json"
    if not path.is_file():
        wanted = _version(path.parent)
        older = [p for p in SCHEMAS_DIR.glob(f"ios-*/{database}.json") if _version(p.parent) < wanted]
        if not older:
            raise FileNotFoundError(f"no {database} schema harvested for iOS {version} or earlier")
        path = max(older, key=lambda p: _version(p.parent))
    return json.loads(path.read_text(encoding="utf-8"))


class Table:
    """Inserts rows, keeping only columns this iOS version's table actually has."""

    def __init__(self, conn: sqlite3.Connection, name: str, columns: list[dict]):
        self.conn, self.name = conn, name
        self.columns = {c["name"] for c in columns}

    def has(self, column: str) -> bool:
        return column in self.columns

    def insert(self, row: dict) -> int:
        kept = {k: v for k, v in row.items() if k in self.columns}
        names = ", ".join(f'"{k}"' for k in kept)
        marks = ", ".join("?" for _ in kept)
        cursor = self.conn.execute(f'INSERT INTO "{self.name}" ({names}) VALUES ({marks})', tuple(kept.values()))
        return cursor.lastrowid


class SchemaDatabase:
    def __init__(self, path: Path, schema: dict):
        self.path, self.schema = Path(path), schema
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(f"PRAGMA page_size = {schema['pragmas']['page_size']}")
        self.conn.execute(f"PRAGMA auto_vacuum = {schema['pragmas']['auto_vacuum']}")
        self._create_tables()

    def _create_tables(self) -> None:
        self._swaps: list[tuple[bytes, bytes]] = []  # (placeholder SQL, real SQL), applied in finish()
        for obj in self.schema["objects"]:
            if obj["type"] == "table":
                self.conn.execute(obj["sql"])
            elif obj["type"] == "virtual":
                try:
                    self.conn.execute(obj["sql"])
                except sqlite3.OperationalError:
                    placeholder = _placeholder_tokenizer(obj["sql"])
                    self.conn.execute(placeholder)
                    self._swaps.append((placeholder.encode("utf-8"), obj["sql"].encode("utf-8")))

    def table(self, name: str) -> Optional[Table]:
        columns = self.schema["columns"].get(name)
        return Table(self.conn, name, columns) if columns is not None else None

    def finish(self) -> Path:
        for obj in self.schema["objects"]:
            if obj["type"] in ("index", "view", "trigger"):
                self.conn.execute(obj["sql"])
        self.conn.execute(f"PRAGMA user_version = {self.schema['pragmas']['user_version']}")
        self.conn.commit()
        if self.schema["pragmas"]["journal_mode"] == "wal":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.close()
        for suffix in ("-wal", "-shm"):
            if os.path.exists(str(self.path) + suffix):
                os.remove(str(self.path) + suffix)
        if self._swaps:
            data = self.path.read_bytes()
            for placeholder, real in self._swaps:
                if data.count(placeholder) != 1:
                    raise RuntimeError(f"expected exactly one copy of the placeholder definition in {self.path.name}")
                data = data.replace(placeholder, real)
            self.path.write_bytes(data)
        return self.path


def build(path: Path, version: str, database: str, populate: Callable[[SchemaDatabase], None]) -> bytes:
    """Create ``database`` for iOS ``version`` at ``path``, fill it, and return the file's bytes."""
    db = SchemaDatabase(path, load_schema(version, database))
    populate(db)
    db.finish()
    return Path(path).read_bytes()


def schema_objects(path: Path) -> Iterable[tuple[str, str, str]]:
    """(type, name, sql) of a built database, for fidelity checks."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        return conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
    finally:
        conn.close()
