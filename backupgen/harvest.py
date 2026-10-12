"""Harvest table definitions and backup-container shape from real backups and Simulator databases.

Only structure is recorded: CREATE statements, column lists, PRAGMAs, plist key
names, keybag layout, plus aggregate column statistics (null rates, storage
types, blob signatures) used to make generated data look like real data. Row
values are never copied.

Output goes to ``schemas/ios-<version>/``:

- ``<database>.json``: objects in creation order with their SQL, columns, PRAGMAs, shape.
- ``container.json``: Manifest/Info/Status plist keys, keybag layout, Manifest.db DDL
  (only when harvesting from a backup).
"""

from __future__ import annotations

import json
import plistlib
import re
import sqlite3
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backupgen import SCHEMAS_DIR

DATABASES = {
    "sms.db": ("HomeDomain", "Library/SMS/sms.db"),
    "AddressBook.sqlitedb": ("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb"),
    "AddressBookImages.sqlitedb": ("HomeDomain", "Library/AddressBook/AddressBookImages.sqlitedb"),
    "CallHistory.storedata": ("HomeDomain", "Library/CallHistoryDB/CallHistory.storedata"),
    "voicemail.db": ("HomeDomain", "Library/Voicemail/voicemail.db"),
    "NoteStore.sqlite": ("AppDomainGroup-group.com.apple.notes", "NoteStore.sqlite"),
    "Photos.sqlite": ("CameraRollDomain", "Media/PhotoData/Photos.sqlite"),
    "History.db": ("HomeDomain", "Library/Safari/History.db"),
    "Calendar.sqlitedb": ("HomeDomain", "Library/Calendar/Calendar.sqlitedb"),
    "healthdb.sqlite": ("HealthDomain", "Health/healthdb.sqlite"),
    "healthdb_secure.sqlite": ("HealthDomain", "Health/healthdb_secure.sqlite"),
    "CloudRecordings.db": ("MediaDomain", "Media/Recordings/CloudRecordings.db"),
}
# Tables SQLite creates for a virtual table itself: FTS3/4/5, then R-tree (Photos' location index).
_SHADOW_SUFFIXES = ("content", "segments", "segdir", "docsize", "stat", "data", "idx", "config", "node", "parent", "rowid")


def _blob_kind(value: Optional[bytes]) -> str:
    if not value:
        return "empty"  # substr() of a zero-length blob is NULL
    if value.startswith(b"bplist00"):
        return "bplist"
    if value[:13] == b"\x04\x0bstreamtyped":
        return "typedstream"
    if value.startswith(b"\x1f\x8b"):
        return "gzip"
    if value[:4] in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb"):
        return "jpeg"
    if value.startswith(b"\x89PNG"):
        return "png"
    return "other"


def harvest_sqlite(path: Path) -> dict:
    """Structure and aggregate shape of one SQLite database. Never reads values out."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    with open(path, "rb") as f:
        header = f.read(100)
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid"
    ).fetchall()
    virtual = {r["name"] for r in rows if r["type"] == "table" and re.search(r"CREATE\s+VIRTUAL\s+TABLE", r["sql"], re.I)}
    shadows = {f"{v}_{s}" for v in virtual for s in _SHADOW_SUFFIXES}
    objects, columns, shape = [], {}, {}
    for r in rows:
        if r["name"].startswith("sqlite_"):
            continue
        kind = "shadow" if r["name"] in shadows else ("virtual" if r["name"] in virtual else r["type"])
        objects.append({"type": kind, "name": r["name"], "table": r["tbl_name"], "sql": r["sql"]})
        if r["type"] != "table" or kind in ("shadow", "virtual"):
            continue  # virtual tables may use iOS-only tokenizers (ab_cf_tokenizer) that can't be opened here
        info = conn.execute(f'PRAGMA table_info("{r["name"]}")').fetchall()
        columns[r["name"]] = [
            {"name": c["name"], "type": c["type"], "notnull": bool(c["notnull"]), "default": c["dflt_value"], "pk": c["pk"]}
            for c in info
        ]
        total = conn.execute(f'SELECT COUNT(*) FROM "{r["name"]}"').fetchone()[0]
        if not total:
            continue
        table_shape = {}
        for c in info:
            name = c["name"]
            types = dict(conn.execute(f'SELECT typeof("{name}"), COUNT(*) FROM "{r["name"]}" GROUP BY 1').fetchall())
            entry = {"null_rate": round(types.get("null", 0) / total, 3), "types": sorted(t for t in types if t != "null")}
            if "blob" in types:
                kinds: dict[str, int] = {}
                for (value,) in conn.execute(f'SELECT substr("{name}", 1, 16) FROM "{r["name"]}" WHERE typeof("{name}") = \'blob\''):
                    kinds[_blob_kind(value)] = kinds.get(_blob_kind(value), 0) + 1
                entry["blob_kinds"] = sorted(kinds)
            table_shape[name] = entry
        shape[r["name"]] = table_shape
    meta = {
        "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "page_size": struct.unpack(">H", header[16:18])[0] or 65536,
        "journal_mode": "wal" if header[18] == 2 else "delete",
        "auto_vacuum": conn.execute("PRAGMA auto_vacuum").fetchone()[0],
        "encoding": conn.execute("PRAGMA encoding").fetchone()[0],
    }
    harvested = {"pragmas": meta, "objects": objects, "columns": columns, "shape": shape}
    if "Z_PRIMARYKEY" in columns:
        # Core Data's entity map (which Z_ENT number is which entity) is model structure, not user data.
        harvested["entities"] = [
            {"Z_ENT": r[0], "Z_NAME": r[1], "Z_SUPER": r[2]}
            for r in conn.execute("SELECT Z_ENT, Z_NAME, Z_SUPER FROM Z_PRIMARYKEY ORDER BY Z_ENT")
        ]
    conn.close()
    return harvested


def _keybag_layout(blob: bytes) -> dict:
    out: dict = {"classes": []}
    i, current = 0, None
    while i + 8 <= len(blob):
        tag = blob[i:i + 4].decode("ascii", "replace")
        size = struct.unpack(">L", blob[i + 4:i + 8])[0]
        value = blob[i + 8:i + 8 + size]
        i += 8 + size
        number = struct.unpack(">L", value)[0] if size == 4 else None
        if tag == "UUID" and "UUID" in out:
            current = {}
            out["classes"].append(current)
            continue
        if current is None:
            out[tag] = number if number is not None else f"bytes[{size}]"
        elif tag in ("CLAS", "WRAP", "KTYP"):
            current[tag] = number
        else:
            current[tag] = f"bytes[{size}]"
    return out


def harvest_container(backup_dir: Path, manifest_db: Path) -> dict:
    """Plist key names, format markers and keybag layout of a real backup. No identifiers or values."""
    manifest = plistlib.load(open(backup_dir / "Manifest.plist", "rb"))
    info = plistlib.load(open(backup_dir / "Info.plist", "rb"))
    status = plistlib.load(open(backup_dir / "Status.plist", "rb"))
    conn = sqlite3.connect(f"{manifest_db.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    ddl = [r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid")]
    conn.close()
    header = manifest_db.read_bytes()[:100]
    return {
        "manifest_plist_keys": sorted(manifest),
        "manifest_version": manifest.get("Version"),
        "system_domains_version": manifest.get("SystemDomainsVersion"),
        "lockdown_keys": sorted(manifest.get("Lockdown", {})),
        "manifest_key_class": struct.unpack("<l", manifest["ManifestKey"][:4])[0] if "ManifestKey" in manifest else None,
        "keybag": _keybag_layout(manifest["BackupKeyBag"]) if "BackupKeyBag" in manifest else None,
        "info_plist_keys": sorted(info),
        "status_plist": {k: (v if k in ("BackupState", "IsFullBackup", "SnapshotState", "Version") else type(v).__name__)
                         for k, v in sorted(status.items())},
        "manifest_db_ddl": ddl,
        "manifest_db_pragmas": {
            "journal_mode": "wal" if header[18] == 2 else "delete",
            "user_version": struct.unpack(">I", header[60:64])[0],
            "page_size": struct.unpack(">H", header[16:18])[0],
        },
    }


def write_schema(ios: str, build: str, source: str, name: str, harvested: dict) -> Path:
    folder = SCHEMAS_DIR / f"ios-{ios}"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "database": name,
        "ios": ios,
        "build": build,
        "source": source,
        "harvested": datetime.now(timezone.utc).date().isoformat(),
        **harvested,
    }
    path = folder / (f"{name}.json" if name != "container" else "container.json")
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def harvest_backup(backup_dir: Path, password: Optional[str], ios: str, build: str, source: str,
                   only: Optional[set[str]] = None) -> list[Path]:
    """Harvest every known database plus the container from an on-disk backup, or just the databases in ``only``."""
    written = []
    with tempfile.TemporaryDirectory(prefix="backupgen-harvest-") as tmp:
        tmp = Path(tmp)
        encrypted = plistlib.load(open(backup_dir / "Manifest.plist", "rb")).get("IsEncrypted")
        if encrypted:
            from iphone_backup_decrypt import EncryptedBackup

            backup = EncryptedBackup(backup_directory=str(backup_dir), passphrase=password)
            manifest_db = tmp / "Manifest.db"
            backup.save_manifest_file(str(manifest_db))
        else:
            backup, manifest_db = None, backup_dir / "Manifest.db"
        lookup = sqlite3.connect(f"{manifest_db.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        for name, (domain, relative_path) in DATABASES.items():
            if only is not None and name not in only:
                continue
            row = lookup.execute(
                "SELECT fileID FROM Files WHERE domain = ? AND relativePath = ? AND flags = 1", (domain, relative_path)
            ).fetchone()
            if not row:
                continue
            target = tmp / name
            if backup is not None:
                backup.extract_file(relative_path=relative_path, domain_like=domain, output_filename=str(target))
            else:
                target.write_bytes((backup_dir / row[0][:2] / row[0]).read_bytes())
            written.append(write_schema(ios, build, source, name, harvest_sqlite(target)))
        lookup.close()
        if only is None:
            written.append(write_schema(ios, build, source, "container", harvest_container(backup_dir, manifest_db)))
    return written
