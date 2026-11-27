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
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen import PERSONAS_DIR  # noqa: E402
from backupgen.container import BackupWriter, Device  # noqa: E402
from backupgen.generate import EXPECTED, generate  # noqa: E402
from backupgen.persona import load_persona  # noqa: E402
from ios_backup_core.backup import open_local_backup  # noqa: E402
from ios_backup_core.contacts import ContactResolver  # noqa: E402
from ios_backup_core.db import connect_readonly  # noqa: E402
from ios_backup_core.extractors.browser_history import BrowserHistoryExtractor  # noqa: E402
from ios_backup_core.extractors.calendar_events import CalendarExtractor  # noqa: E402
from ios_backup_core.extractors.calls import CallExtractor  # noqa: E402
from ios_backup_core.extractors.messages import MessageExtractor  # noqa: E402
from ios_backup_core.extractors.notes import NoteExtractor  # noqa: E402
from ios_backup_core.extractors.photos import PhotoExtractor  # noqa: E402
from ios_backup_core.extractors.voice_memos import VoiceMemoExtractor  # noqa: E402
from ios_backup_core.extractors.voicemail import VoicemailExtractor  # noqa: E402
from ios_backup_core.extractors.whatsapp import WhatsAppExtractor  # noqa: E402

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
    recordings = VoiceMemoExtractor()
    memos = recordings.list_voice_memos(accessor)
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
        "whatsapp": WhatsAppExtractor().list_conversations(accessor, lookup),
        "calendars": CalendarExtractor().list_calendars(accessor),
        "calendar_events": CalendarExtractor().list_events(accessor, contacts=lookup),
        "voice_memos": memos,
        # Audio files and the transcripts inside them are opened too.
        "voice_memo_audio": [recordings.get_audio_path(accessor, m["id"]) for m in memos["voice_memos"]],
        "transcripts": [recordings.get_transcript(accessor, m["id"]) for m in memos["voice_memos"]],
    }


# Made up: a calendar and a few recordings for the test persona, so Calendar.sqlitedb and
# CloudRecordings.db (WAL databases on a phone) and the recordings' audio are read too.
_CALENDAR = {
    "calendars": [{"id": "cal", "title": "Calendar", "color": "#1BADF8", "account": "icloud"}],
    "events": [
        {"id": "standup", "calendar": "cal", "title": "Standup", "start": "2025-06-03T09:30", "end": "2025-06-03T09:45",
         "created": "2025-05-28T16:00", "organizer": "me", "attendees": ["riley"], "alerts": [5],
         "repeat": {"frequency": "weekly", "count": 6}},
        {"id": "trip", "calendar": "cal", "title": "Lisbon", "start": "2025-07-14", "end": "2025-07-18",
         "created": "2025-05-01T19:00"},
    ],
}
_VOICE_MEMOS = """== 2025-03-10
08:15 20s Shopping list (favorite) (folder: Lists)
== 2025-09-01
18:00 15s Song idea
  > A short tune for the morning.
"""


def _pack(tmp_path: Path):
    folder = tmp_path / "packs" / "testpersona"
    shutil.copytree(PERSONAS_DIR / "testpersona", folder)
    (folder / "calendar.json").write_text(json.dumps(_CALENDAR), encoding="utf-8")
    (folder / "voice_memos.txt").write_text(_VOICE_MEMOS, encoding="utf-8")
    return load_persona("testpersona", root=folder.parent)


def _generate(tmp_path: Path):
    # Spaces, '#' and '%' are special in the file: URIs used to open databases.
    out = tmp_path / "Backups #1 100% ü"
    backup_dir = generate("testpersona", "ios26", out, persona=_pack(tmp_path))
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
    # The voicemail list leaves out the Deleted list unless asked; the answer key holds every list.
    assert len(data["voicemails"]["voicemails"]) == len([v for v in expected["voicemails"] if not v["is_deleted"]])
    assert data["whatsapp"] == {"conversations": [], "empty_reason": "not_in_backup"}
    assert len(data["calendars"]["calendars"]) == len(expected["calendar"]["calendars"])
    assert len(data["calendar_events"]["events"]) == len(expected["calendar"]["events"])
    assert len(data["voice_memos"]["voice_memos"]) == len(expected["voice_memos"])
    assert all(path and os.path.isfile(path) for path in data["voice_memo_audio"])
    assert [t and t["text"] for t in data["transcripts"]] == [m["transcript"] for m in expected["voice_memos"]]


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


def _backup_with(tmp_path: Path, files: dict, password=None) -> Path:
    device = Device(name="Test iPhone", product_type="iPhone15,4", product_name="iPhone 15",
                    ios="17.3", build="21D50", phone_number="+44 7700 900001")
    writer = BackupWriter(tmp_path / "out", device, datetime(2024, 2, 1, tzinfo=timezone.utc),
                          "17.3", password=password, seed=1)
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


def test_a_decrypted_wal_lands_beside_its_database(tmp_path):
    """Decrypted files get hashed names in the temp dir. A database's -wal gets
    the matching name, so the database is read where it was decrypted to
    instead of being copied a second time (sms.db can run to gigabytes)."""
    main, wal = _wal_database(tmp_path)
    backup_dir = _backup_with(tmp_path, {TEST_DB: main, TEST_DB + "-wal": wal}, password="pw")
    accessor = open_local_backup(str(backup_dir), password="pw")
    db_path = accessor.get_database(TEST_DB)
    assert db_path == accessor.get_file(TEST_DB), "read in place, not copied"
    assert _rows(accessor) == [1, 2]


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


# ── Files that go away while the backup is open (OE-77) ─────────────────────
#
# OE-77: the home screen read voicemail.db, then the Voicemail list couldn't
# open it ("unable to open database file"). One way that happens is a file
# found earlier going away: a temp-folder cleaner removing the copies, or the
# drive with the backup going to sleep. A path that has gone is looked up
# again, and the voicemail list tries again before giving up.

VOICEMAIL_DB = "Library/Voicemail/voicemail.db"


def _wal_voicemail_db(tmp_path: Path) -> tuple:
    """voicemail.db as a phone leaves it: two voicemails in the file, a third
    only in its -wal. Returns (main, wal) bytes."""
    path = tmp_path / "vm-build" / "voicemail.db"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE voicemail (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, remote_uid INTEGER, "
        "date INTEGER, token TEXT, sender TEXT, callback_num TEXT, duration INTEGER, "
        "expiration INTEGER, trashed_date INTEGER, flags INTEGER, receiver TEXT, "
        "label TEXT, uuid TEXT)"
    )
    conn.execute("CREATE INDEX date_index on voicemail(date)")
    insert = ("INSERT INTO voicemail (remote_uid, date, token, sender, duration, trashed_date, flags) "
              "VALUES (?, ?, 'Complete', ?, ?, 0, 3)")
    conn.execute(insert, (101, 1_700_000_000, "+15550000001", 30))
    conn.execute(insert, (102, 1_700_100_000, "+15550000002", 45))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(insert, (103, 1_700_200_000, "+15550000003", 60))
    conn.commit()
    main, wal = path.read_bytes(), Path(f"{path}-wal").read_bytes()
    conn.close()
    assert wal, "the third voicemail should be in the WAL"
    return main, wal


def _voicemail_backup(tmp_path: Path, password=None) -> Path:
    main, wal = _wal_voicemail_db(tmp_path)
    return _backup_with(tmp_path, {VOICEMAIL_DB: main, VOICEMAIL_DB + "-wal": wal}, password=password)


def _senders(result: dict) -> list:
    assert "error" not in result, result.get("error")
    return [v["phone_number"] for v in result["voicemails"]]


ALL_THREE = ["+15550000003", "+15550000002", "+15550000001"]


def test_a_copy_gone_from_the_temp_dir_is_made_again(tmp_path):
    main, wal = _wal_database(tmp_path)
    accessor = open_local_backup(str(_backup_with(tmp_path, {TEST_DB: main, TEST_DB + "-wal": wal})))
    first = accessor.get_database(TEST_DB)
    shutil.rmtree(accessor._temp_dir)
    again = accessor.get_database(TEST_DB)
    assert os.path.isfile(again) and os.path.isfile(again + "-wal")
    assert again != first
    assert _rows(accessor) == [1, 2]


def test_a_decrypted_file_gone_from_the_temp_dir_is_decrypted_again(tmp_path):
    main, wal = _wal_database(tmp_path)
    backup_dir = _backup_with(tmp_path, {TEST_DB: main, TEST_DB + "-wal": wal}, password="pw")
    accessor = open_local_backup(str(backup_dir), password="pw")
    assert _rows(accessor) == [1, 2]
    shutil.rmtree(accessor._temp_dir)
    assert os.path.isfile(accessor.get_file(TEST_DB))
    assert _rows(accessor) == [1, 2]


@pytest.mark.parametrize("password", [None, "pw"])
def test_voicemail_still_lists_after_the_temp_dir_is_emptied(tmp_path, password):
    """The OE-77 sequence: voicemail.db is read once (the home screen's numbers),
    the temp copies go, and the Voicemail list is opened."""
    accessor = open_local_backup(str(_voicemail_backup(tmp_path, password)), password=password)
    extractor = VoicemailExtractor()
    assert _senders(extractor.list_voicemails(accessor, {})) == ALL_THREE
    shutil.rmtree(accessor._temp_dir)
    assert _senders(extractor.list_voicemails(accessor, {})) == ALL_THREE


def test_voicemail_falls_back_to_the_file_without_its_wal(tmp_path, monkeypatch):
    """If even a fresh copy with its -wal can't be read, the backup's own file
    is: the voicemails saved before the last checkpoint still show."""
    accessor = open_local_backup(str(_voicemail_backup(tmp_path)))
    real_read = VoicemailExtractor._read_rows

    def read(db_path, backup, ignore_wal):
        if not ignore_wal:
            raise sqlite3.OperationalError("unable to open database file")
        return real_read(db_path, backup, ignore_wal)

    monkeypatch.setattr(VoicemailExtractor, "_read_rows", staticmethod(read))
    assert _senders(VoicemailExtractor().list_voicemails(accessor, {})) == ALL_THREE[1:]


def test_voicemail_gone_from_the_backup_says_so_for_people(tmp_path):
    """The backup's files went away after voicemail.db was found (a drive
    unplugged): a sentence for people, not SQLite's "unable to open database file"."""
    main, _ = _wal_voicemail_db(tmp_path)
    accessor = open_local_backup(str(_backup_with(tmp_path, {VOICEMAIL_DB: main})))
    os.remove(accessor.get_database(VOICEMAIL_DB))
    result = VoicemailExtractor().list_voicemails(accessor, {})
    assert result["voicemails"] == []
    assert "empty_reason" not in result
    assert result["error"].startswith("Voicemail is in this backup but could not be read.")


def test_voicemail_that_cant_be_opened_says_so_for_people(tmp_path):
    """voicemail.db is there but SQLite can't open it: say what to check, and
    keep SQLite's reason for reports."""
    main, _ = _wal_voicemail_db(tmp_path)
    backup_dir = _backup_with(tmp_path, {VOICEMAIL_DB: main})
    accessor = open_local_backup(str(backup_dir))
    db_path = Path(accessor.get_database(VOICEMAIL_DB))
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("file permissions aren't enforced for this user")
    db_path.chmod(0)
    try:
        result = VoicemailExtractor().list_voicemails(accessor, {})
    finally:
        db_path.chmod(0o644)
    assert result["voicemails"] == []
    assert "empty_reason" not in result
    assert result["error"].startswith("Voicemail is in this backup but couldn't be opened.")
    assert "still connected" in result["error"]
    assert result["error"].endswith("(unable to open database file)")
