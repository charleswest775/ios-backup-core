"""Reading a backup must never change it, and must work when it is read-only.

Backups sit on read-only disks, in another user's folder, or behind macOS
permissions. Most iOS databases, Manifest.db included, are WAL databases, and a
plain sqlite3.connect() on one creates -wal/-shm files beside it: that fails in
a read-only folder, so every lookup came back "not found", and otherwise it
writes into the user's backup.
"""

import hashlib
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen.container import BackupWriter, Device  # noqa: E402
from backupgen.generate import EXPECTED, generate  # noqa: E402
from ios_backup_core.backup import open_local_backup  # noqa: E402
from ios_backup_core.contacts import ContactResolver  # noqa: E402
from ios_backup_core.db import connect_readonly  # noqa: E402
from ios_backup_core.extractors.browser_history import BrowserHistoryExtractor  # noqa: E402
from ios_backup_core.extractors.calls import CallExtractor  # noqa: E402
from ios_backup_core.extractors.messages import MessageExtractor  # noqa: E402
from ios_backup_core.extractors.notes import NoteExtractor  # noqa: E402
from ios_backup_core.extractors.photos import PhotoExtractor  # noqa: E402
from ios_backup_core.extractors.voicemail import VoicemailExtractor  # noqa: E402

TEST_DB = "Library/Test/test.db"


def _snapshot(folder: Path) -> dict:
    """Every file under *folder*, mapped to a hash of its contents."""
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*")) if p.is_file()
    }


@contextmanager
def _read_only(folder: Path):
    """Make *folder* and everything in it read-only, like a backup on a locked disk."""
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("folder permissions aren't enforced for this user")
    paths = [folder, *folder.rglob("*")]
    for p in paths:
        p.chmod(p.stat().st_mode & ~0o222)
    try:
        yield
    finally:
        for p in paths:
            p.chmod(p.stat().st_mode | 0o200)


def _read_everything(backup_dir: Path) -> dict:
    accessor = open_local_backup(str(backup_dir))
    resolver = ContactResolver()
    lookup = resolver.load_contacts(accessor)
    extractor = MessageExtractor()
    conversations = extractor.list_conversations(accessor, lookup)
    messages = [
        m
        for conv in conversations["conversations"]
        for m in extractor.get_messages(accessor, conv["chat_id"], lookup, 0, 10_000)["messages"]
    ]
    return {
        "contacts": resolver.list_contacts(accessor),
        "conversations": conversations,
        "messages": messages,
        "search": extractor.search_messages(accessor, "a", lookup),
        "photos": PhotoExtractor().list_photos(accessor, 0, 100),
        "albums": PhotoExtractor().list_albums(accessor),
        "notes": NoteExtractor().list_notes(accessor),
        "voicemails": VoicemailExtractor().list_voicemails(accessor, lookup),
        "calls": CallExtractor().list_calls(accessor, lookup),
        "browser_history": BrowserHistoryExtractor().list_browser_history(accessor),
    }


def _generate(tmp_path: Path):
    # Spaces, '#' and '%' are special in the file: URIs used to open databases.
    out = tmp_path / "Backups #1 100% ü"
    backup_dir = generate("testpersona", "ios26", out)
    return backup_dir, json.loads((out / EXPECTED).read_text(encoding="utf-8"))


def test_a_read_only_backup_reads_in_full(tmp_path):
    backup_dir, expected = _generate(tmp_path)
    with _read_only(backup_dir):
        data = _read_everything(backup_dir)

    assert "error" not in data["conversations"]
    assert len(data["contacts"]["contacts"]) == len(expected["contacts"])
    assert {c["chat_identifier"] for c in data["conversations"]["conversations"]} == {
        c["chat_identifier"] for c in expected["conversations"]
    }
    assert len(data["messages"]) == len(expected["messages"])
    assert data["photos"]["source"] != "unavailable"
    assert len(data["photos"]["photos"]) == len([p for p in expected["photos"] if not p["trashed"]])
    assert len(data["notes"]["notes"]) == len(expected["notes"])
    assert len(data["voicemails"]["voicemails"]) == len(expected["voicemails"])


def test_reading_leaves_the_backup_unchanged(tmp_path):
    backup_dir, _ = _generate(tmp_path)
    before = _snapshot(backup_dir)
    _read_everything(backup_dir)
    # No -shm/-wal files beside the databases, and no indexes written into sms.db.
    assert _snapshot(backup_dir) == before


# ── WAL files ────────────────────────────────────────────────────────────────


def _wal_database(tmp_path: Path) -> tuple:
    """A WAL database whose second row is only in its -wal. Returns (main, wal) bytes."""
    path = tmp_path / "build" / "test.db"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO t VALUES (2)")
    conn.commit()
    main, wal = path.read_bytes(), Path(f"{path}-wal").read_bytes()
    conn.close()
    assert wal, "the second row should be in the WAL"
    return main, wal


def _backup_with(tmp_path: Path, files: dict) -> Path:
    device = Device(name="Test iPhone", product_type="iPhone15,4", product_name="iPhone 15",
                    ios="17.3", build="21D50", phone_number="+44 7700 900001")
    writer = BackupWriter(tmp_path / "out", device, datetime(2024, 2, 1, tzinfo=timezone.utc),
                          "17.3", password=None, seed=1)
    for rel_path, data in files.items():
        writer.add_file("HomeDomain", rel_path, data)
    return writer.finalize()


def _rows(accessor) -> list:
    conn = connect_readonly(accessor.get_database(TEST_DB), accessor)
    try:
        return [r[0] for r in conn.execute("SELECT x FROM t ORDER BY x")]
    finally:
        conn.close()


@pytest.mark.parametrize("read_only", [False, True])
def test_a_wal_in_the_backup_is_merged_from_a_copy(tmp_path, read_only):
    """Each file in an unencrypted backup is stored under its own hash, so a
    database's -wal never sits beside it. Its rows are still read."""
    main, wal = _wal_database(tmp_path)
    backup_dir = _backup_with(tmp_path, {TEST_DB: main, TEST_DB + "-wal": wal})
    before = _snapshot(backup_dir)
    if read_only:
        with _read_only(backup_dir):
            assert _rows(open_local_backup(str(backup_dir))) == [1, 2]
    else:
        assert _rows(open_local_backup(str(backup_dir))) == [1, 2]
    assert _snapshot(backup_dir) == before


def test_a_stray_wal_beside_a_backup_file_is_left_alone(tmp_path):
    """OpenExtract 0.6 and older could leave <hash>-wal/-shm files in a backup.
    They are not part of it: don't read them, and don't write to them."""
    main, wal = _wal_database(tmp_path)
    backup_dir = _backup_with(tmp_path, {TEST_DB: main})
    accessor = open_local_backup(str(backup_dir))
    db_path = accessor.get_database(TEST_DB)
    Path(f"{db_path}-wal").write_bytes(wal)
    before = _snapshot(backup_dir)
    assert _rows(accessor) == [1]
    assert _snapshot(backup_dir) == before
    assert not Path(f"{db_path}-shm").exists()


def test_a_non_database_raises():
    with pytest.raises(sqlite3.DatabaseError):
        connect_readonly(__file__)


@pytest.mark.parametrize("path, uri", [
    (r"C:\Users\Ann\Apple\MobileSync\Backup\x\Manifest.db",
     "file:///C:/Users/Ann/Apple/MobileSync/Backup/x/Manifest.db"),
    # A network share: SQLite rejects any URI authority, so it must stay empty.
    (r"\\nas\backups\x\Manifest.db", "file:////nas/backups/x/Manifest.db"),
    (r"D:\Backups #1 100%\Manifest.db", "file:///D:/Backups%20%231%20100%25/Manifest.db"),
])
def test_windows_paths_make_valid_sqlite_uris(monkeypatch, path, uri):
    import ntpath

    from ios_backup_core import db

    monkeypatch.setattr(db.os, "path", ntpath)
    monkeypatch.setattr(db.os, "sep", "\\")
    assert db._file_uri(path) == uri
