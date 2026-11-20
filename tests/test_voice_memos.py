"""Tests for VoiceMemoExtractor.

Three layouts of CloudRecordings.db are covered:

- model 6 (iOS 12 to 13): built from the schema harvested from an iOS 13.4.1
  backup (``backupgen/schemas/ios-13.4.1/CloudRecordings.db.json``), no
  folders, favourites or local duration;
- model 10 (iOS 17) and model 14 (iOS 26.5): column sets written from Apple's
  decoded Core Data models (SQL names are ``Z`` plus the upper-cased attribute;
  the ``folder`` relationship is ``ZFOLDER``), with the ``ZFOLDER`` table.

Audio files are tiny MPEG-4 files built in the test (transcripts as Voice Memos
writes them); nothing here decodes audio. All data is made up.
"""

import json
import os
import re
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen.container import BackupWriter, Device
from backupgen.sqlitegen import build
from ios_backup_core.backup import open_local_backup
from ios_backup_core.extractors.voice_memos import (
    MEDIA_DB_PATH,
    MEDIA_DOMAIN,
    VOICE_MEMOS_DB_PATH,
    VOICE_MEMOS_DOMAIN,
    VoiceMemoExtractor,
)

NOT_READABLE = "Voice Memos are in this backup but could not be read."
UNSUPPORTED = "This backup's Voice Memos are in an older format OpenExtract can't read yet."
FOLDER = "Recordings"
MEDIA_FOLDER = "Media/Recordings"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _apple(iso: str) -> float:
    return (datetime.fromisoformat(iso).replace(tzinfo=timezone.utc) - APPLE_EPOCH).total_seconds()


# ---------------------------------------------------------------------------
# A fake accessor
# ---------------------------------------------------------------------------

class _FakeBackup:
    """Accessor stub keyed by (domain, path). ``unreadable`` entries are in the
    manifest but can't be extracted. Every get_file call is recorded."""

    def __init__(self, files=None, unreadable=()):
        self.files = dict(files or {})
        self.unreadable = set(unreadable)
        self.fetched = []

    def get_file(self, relative_path, domain="HomeDomain"):
        self.fetched.append((domain, relative_path))
        return self.files.get((domain, relative_path))

    def list_files(self, domain=None, path_like=None):
        entries = [*self.files, *self.unreadable]
        if domain:
            entries = [e for e in entries if e[0] == domain]
        if path_like:
            pattern = ".*".join(re.escape(p) for p in path_like.split("%"))
            entries = [e for e in entries if re.fullmatch(pattern, e[1], re.IGNORECASE)]
        return [{"hash": "", "domain": d, "path": p} for d, p in entries]


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------

# Model 10 (iOS 17): encrypted title back, custom label for sorting, local duration.
MODEL10 = (
    "Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZFLAGS INTEGER, ZFOLDER INTEGER,"
    " ZSHAREDFLAGS INTEGER, ZDATE TIMESTAMP, ZDURATION FLOAT, ZEVICTIONDATE TIMESTAMP,"
    " ZLOCALDURATION FLOAT, ZCUSTOMLABEL VARCHAR, ZCUSTOMLABELFORSORTING VARCHAR,"
    " ZENCRYPTEDTITLE VARCHAR, ZPATH VARCHAR, ZUNIQUEID VARCHAR, ZAUDIODIGEST BLOB, ZAUDIOFUTURE BLOB"
)
# Model 14 (iOS 26.5): model 10 plus playback, layered (mt), studio mix and versioned audio.
MODEL14 = MODEL10 + (
    ", ZAUDIOFUTUREFLAGS INTEGER, ZSILENCEREMOVERENABLED INTEGER, ZSKIPSILENCEENABLED INTEGER,"
    " ZSTUDIOMIXENABLED INTEGER, ZMTLAYERMIX FLOAT, ZPLAYBACKPOSITION FLOAT, ZPLAYBACKRATE FLOAT,"
    " ZPLAYBACKSPEED FLOAT, ZSTUDIOMIXLEVEL FLOAT, ZAUDIOFUTUREUUIDS BLOB, ZMTAUDIOFUTURE BLOB,"
    " ZVERSIONEDAUDIOFUTURE BLOB"
)
MODELS = {"model10": MODEL10, "model14": MODEL14}
FOLDER_TABLE = (
    "Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCOUNTOFRECORDINGS INTEGER,"
    " ZRANK INTEGER, ZENCRYPTEDNAME VARCHAR, ZUUID VARCHAR"
)
# The rest of a model 10+ store: other entities and Core Data's own tables.
OTHER_TABLES = (
    "CREATE TABLE ZDATABASEPROPERTY (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZKEY VARCHAR, ZVALUE VARCHAR)",
    ("CREATE TABLE ZENTITYREVISION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZRECORDING_ID INTEGER,"
     " ZREVISION_ID INTEGER, ZREVISION_TYPE INTEGER, ZUNIQUEID VARCHAR)"),
    "CREATE TABLE ZMIGRATION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZFLAGS INTEGER, ZDATE TIMESTAMP)",
    ("CREATE TABLE ZRECORDING (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZITUNESPERSISTENTID INTEGER,"
     " ZLABELPRESET INTEGER, ZPENDINGRESTORE INTEGER, ZRECORDINGID INTEGER, ZSYNCED INTEGER, ZDATE TIMESTAMP,"
     " ZDURATION FLOAT, ZCUSTOMLABEL VARCHAR, ZPATH VARCHAR, ZUNIQUEID VARCHAR)"),
    "CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER)",
    "CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB)",
    "CREATE TABLE Z_MODELCACHE (Z_CONTENT BLOB)",
)


def _insert(conn, table, row):
    names = ", ".join(row)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


def _store(path, recording_cols=MODEL14, recordings=(), folders=(), folder_cols=FOLDER_TABLE,
           other_tables=OTHER_TABLES) -> str:
    """A CloudRecordings.db with the given ZCLOUDRECORDING columns and rows."""
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE ZCLOUDRECORDING ({recording_cols})")
    if folder_cols:
        conn.execute(f"CREATE TABLE ZFOLDER ({folder_cols})")
    for ddl in other_tables:
        conn.execute(ddl)
    for row in recordings:
        _insert(conn, "ZCLOUDRECORDING", row)
    for row in folders:
        _insert(conn, "ZFOLDER", row)
    conn.commit()
    conn.close()
    return str(path)


def _harvested(path, table="ZCLOUDRECORDING", rows=()) -> str:
    """A database with the iOS 13.4.1 harvested schema (model 6; both tables)."""
    def fill(db):
        for row in rows:
            db.table(table).insert(row)
    build(Path(path), "13.4.1", "CloudRecordings.db", fill)
    return str(path)


def _db_file(tmp_path, name="CloudRecordings.db"):
    folder = tmp_path / f"db{len(list(tmp_path.glob('db*')))}"
    folder.mkdir()
    return folder / name


# ---------------------------------------------------------------------------
# Audio files
# ---------------------------------------------------------------------------

def _box(kind: bytes, *payload: bytes) -> bytes:
    body = b"".join(payload)
    return struct.pack(">I", 8 + len(body)) + kind + body


def _m4a(transcript=None) -> bytes:
    """A tiny .m4a: one enabled sound track, with ``moov/trak/udta/tsrp`` when given (text, start, end) runs."""
    track = [
        _box(b"tkhd", struct.pack(">I", 1), bytes(80)),
        _box(b"mdia", _box(b"hdlr", bytes(8), b"soun", bytes(12), b"\x00"),
             _box(b"minf", _box(b"stbl", _box(b"stsd", struct.pack(">II", 0, 1), struct.pack(">I", 36), b"mp4a", bytes(28))))),
    ]
    if transcript:
        runs, table = [], []
        for i, (text, start, end) in enumerate(transcript):
            runs += [text, i]
            table.append({"timeRange": [start, end]})
        doc = {"attributedString": {"attributeTable": table, "runs": runs}, "locale": {"identifier": "en_US", "current": 0}}
        track.append(_box(b"udta", _box(b"tsrp", json.dumps(doc).encode("utf-8"))))
    moov = _box(b"moov", _box(b"mvhd", bytes(100)), _box(b"trak", *track))
    return _box(b"ftyp", b"M4A ", bytes(4)) + moov + _box(b"mdat", bytes(128))


def _audio(tmp_path, name, content=None) -> str:
    folder = tmp_path / "audio"
    folder.mkdir(exist_ok=True)
    path = folder / name.replace("/", "_")
    path.write_bytes(_m4a() if content is None else content)
    return str(path)


# ---------------------------------------------------------------------------
# The main fixture: an iOS 17 / iOS 26 library
# ---------------------------------------------------------------------------

UID_WALK = "6F3B2C1A-0D4E-4A51-9C77-3E2B1F0A9D88"
FILE_WALK = "20260513 141522-6F3B2C1A.m4a"
WALK_TRANSCRIPT = [("Check", 0.0, 0.8), (" the north", 0.8, 1.5), (" stairwell.", 1.5, 2.2)]

FOLDERS = [
    {"Z_PK": 1, "ZUUID": "F0A1-SITE", "ZENCRYPTEDNAME": "Site visits", "ZRANK": 1, "ZCOUNTOFRECORDINGS": 2},
    {"Z_PK": 2, "ZUUID": "F0A2-SONG", "ZENCRYPTEDNAME": "Song ideas", "ZRANK": 0, "ZCOUNTOFRECORDINGS": 1},
    {"Z_PK": 3, "ZUUID": "F0A3-EMPTY", "ZENCRYPTEDNAME": "Empty", "ZRANK": 1, "ZCOUNTOFRECORDINGS": 0},
]

RECORDINGS = [
    # Favourite, in a folder, transcribed. ZCUSTOMLABEL holds iOS 17's date placeholder.
    {"Z_PK": 1, "ZUNIQUEID": UID_WALK, "ZDATE": _apple("2026-05-13T21:15:22"), "ZDURATION": 83.0,
     "ZLOCALDURATION": 83.4, "ZCUSTOMLABELFORSORTING": "Site walk", "ZENCRYPTEDTITLE": "Site walk",
     "ZCUSTOMLABEL": "13 May 2026 at 14:15", "ZPATH": FILE_WALK, "ZFOLDER": 1, "ZSHAREDFLAGS": 0x1,
     "ZFLAGS": 0x4 | 0x200 | 0x400},
    # Recently Deleted, still in its folder.
    {"Z_PK": 2, "ZUNIQUEID": "22223333-AAAA-4BBB-8CCC-DDDDEEEEFFFF", "ZDATE": _apple("2026-04-02T08:00:00"),
     "ZDURATION": 12.5, "ZENCRYPTEDTITLE": "Old idea", "ZPATH": "20260402 010000-22223333.m4a",
     "ZFOLDER": 1, "ZFLAGS": 0x4, "ZEVICTIONDATE": _apple("2026-05-01T10:00:00")},
    # Only in iCloud: no file, ZFLAGS 0x2.
    {"Z_PK": 3, "ZUNIQUEID": "33334444-AAAA-4BBB-8CCC-DDDDEEEEFFFF", "ZDATE": _apple("2026-03-01T17:00:00"),
     "ZDURATION": 40.0, "ZENCRYPTEDTITLE": "Kept in iCloud", "ZPATH": "20260301 090000-33334444.m4a",
     "ZFLAGS": 0x2},
    # No ZPATH: found by the first 8 characters of its unique id (lower case on disk).
    {"Z_PK": 4, "ZUNIQUEID": "AB12CD34-AAAA-4BBB-8CCC-DDDDEEEEFFFF", "ZDATE": _apple("2026-02-20T20:00:00"),
     "ZDURATION": 5.0, "ZENCRYPTEDTITLE": "No path", "ZPATH": None, "ZFLAGS": 0x4},
    # A full device path from before the move to the app group.
    {"Z_PK": 5, "ZUNIQUEID": "55556666-AAAA-4BBB-8CCC-DDDDEEEEFFFF", "ZDATE": _apple("2025-01-01T18:10:10"),
     "ZDURATION": 61.0, "ZENCRYPTEDTITLE": "Old path",
     "ZPATH": "/var/mobile/Media/Recordings/20250101 101010-55556666.m4a", "ZFLAGS": 0x4},
    # Layered, in a .qta, in the other folder.
    {"Z_PK": 6, "ZUNIQUEID": "66667777-AAAA-4BBB-8CCC-DDDDEEEEFFFF", "ZDATE": _apple("2026-01-10T18:00:00"),
     "ZDURATION": 30.0, "ZENCRYPTEDTITLE": "Layered", "ZPATH": "20260110 100000-66667777.qta",
     "ZFOLDER": 2, "ZFLAGS": 0x4 | 0x800},
    # Spatial, in a .qt.
    {"Z_PK": 7, "ZUNIQUEID": "77778888-AAAA-4BBB-8CCC-DDDDEEEEFFFF", "ZDATE": _apple("2026-01-11T18:00:00"),
     "ZDURATION": 31.0, "ZENCRYPTEDTITLE": "Spatial", "ZPATH": "20260111 100000-77778888.qt",
     "ZFLAGS": 0x4 | 0x1000},
    # Recorded on Watch (local flag), enhanced.
    {"Z_PK": 8, "ZUNIQUEID": "88889999-AAAA-4BBB-8CCC-DDDDEEEEFFFF", "ZDATE": _apple("2026-01-12T18:00:00"),
     "ZDURATION": 32.0, "ZENCRYPTEDTITLE": "Watch", "ZPATH": "20260112 100000-88889999.m4a",
     "ZFLAGS": 0x4 | 0x40, "ZSHAREDFLAGS": 0x2},
    # Recorded on watchOS (synced flag).
    {"Z_PK": 9, "ZUNIQUEID": "9999AAAA-AAAA-4BBB-8CCC-DDDDEEEEFFFF", "ZDATE": _apple("2026-01-13T18:00:00"),
     "ZDURATION": 33.0, "ZENCRYPTEDTITLE": "Watch synced", "ZPATH": "20260113 100000-9999AAAA.m4a",
     "ZFLAGS": 0x4, "ZSHAREDFLAGS": 0x40},
    # Playable, but its file isn't in the backup.
    {"Z_PK": 10, "ZUNIQUEID": "AAAABBBB-AAAA-4BBB-8CCC-DDDDEEEEFFFF", "ZDATE": _apple("2025-06-01T12:00:00"),
     "ZDURATION": 9.0, "ZENCRYPTEDTITLE": "Gone", "ZPATH": "20250601 050000-AAAABBBB.m4a", "ZFLAGS": 0x4},
    # No title, no date, no flags.
    {"Z_PK": 11, "ZUNIQUEID": None, "ZDATE": None, "ZDURATION": None, "ZPATH": None, "ZFLAGS": None},
]

PRESENT_AUDIO = [
    FILE_WALK, "20260402 010000-22223333.m4a", "20260220 120000-ab12cd34.m4a",
    "20250101 101010-55556666.m4a", "20260110 100000-66667777.qta", "20260111 100000-77778888.qt",
    "20260112 100000-88889999.m4a", "20260113 100000-9999AAAA.m4a",
]
# Files beside them that are not recordings, or not in the recordings folder itself.
NOISE = [
    "20260513 141522-6F3B2C1A.waveform",
    "20260513 141522-6F3B2C1A.composition/manifest.plist",
    "20260513 141522-6F3B2C1A.composition/fragments/20260301 090000-33334444.m4a",
    "Sub/20260301 090000-33334444.m4a",
    "20250601 050000-AAAABBBB.m4a.waveform",
]


def _library(tmp_path, recording_cols=MODEL14, domain=VOICE_MEMOS_DOMAIN, folder=FOLDER,
             recordings=RECORDINGS, folders=FOLDERS):
    db = _store(_db_file(tmp_path), recording_cols, recordings, folders)
    files = {(domain, f"{folder}/CloudRecordings.db"): db}
    for name in PRESENT_AUDIO:
        content = _m4a(WALK_TRANSCRIPT) if name == FILE_WALK else None
        files[(domain, f"{folder}/{name}")] = _audio(tmp_path, f"{domain}-{name}", content)
    for name in NOISE:
        files[(domain, f"{folder}/{name}")] = _audio(tmp_path, f"{domain}-noise-{name}")
    return _FakeBackup(files)


@pytest.fixture(params=sorted(MODELS))
def library(request, tmp_path):
    return _library(tmp_path, MODELS[request.param])


def _by_id(result):
    return {m["id"]: m for m in result["voice_memos"]}


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def test_lists_recordings_newest_first(library):
    result = VoiceMemoExtractor().list_voice_memos(library)
    assert "error" not in result and "empty_reason" not in result
    assert [m["id"] for m in result["voice_memos"]] == [1, 2, 3, 4, 9, 8, 7, 6, 10, 5, 11]


def test_full_record(library):
    walk = _by_id(VoiceMemoExtractor().list_voice_memos(library))[1]
    assert walk == {
        "id": 1,
        "unique_id": UID_WALK,
        "title": "Site walk",
        "date": "2026-05-13T21:15:22+00:00",
        "duration_seconds": 83.4,
        "folder_id": "F0A1-SITE",
        "folder": "Site visits",
        "is_favorite": True,
        "is_deleted": False,
        "deleted_date": None,
        "in_phone_list": True,
        "recorded_on_watch": False,
        "is_enhanced": False,
        "is_layered": False,
        "is_spatial": False,
        "has_transcript": True,
        "file_name": FILE_WALK,
        "format": "m4a",
        "audio_available": True,
        "audio_missing_reason": None,
        "source": "CloudRecordings.db",
    }


def test_folders_in_rank_order_with_counts(library):
    result = VoiceMemoExtractor().list_voice_memos(library)
    assert result["folders"] == [
        {"id": "F0A2-SONG", "name": "Song ideas", "rank": 0, "count": 1},
        {"id": "F0A3-EMPTY", "name": "Empty", "rank": 1, "count": 0},
        # The deleted recording in it isn't counted.
        {"id": "F0A1-SITE", "name": "Site visits", "rank": 1, "count": 1},
    ]
    memos = _by_id(result)
    assert memos[6]["folder"] == "Song ideas" and memos[6]["folder_id"] == "F0A2-SONG"
    assert memos[3]["folder"] is None and memos[3]["folder_id"] is None


def test_deleted_recording(library):
    old = _by_id(VoiceMemoExtractor().list_voice_memos(library))[2]
    assert old["is_deleted"] is True
    assert old["deleted_date"] == "2026-05-01T10:00:00+00:00"
    assert old["folder"] == "Site visits"
    assert old["audio_available"] is True


def test_audio_presence_and_reasons(library):
    memos = _by_id(VoiceMemoExtractor().list_voice_memos(library))
    # Only in iCloud. A same-named file in a subfolder or an edit's .composition doesn't count.
    assert memos[3]["audio_available"] is False
    assert memos[3]["audio_missing_reason"] == "in_icloud_only"
    assert memos[3]["file_name"] == "20260301 090000-33334444.m4a" and memos[3]["format"] == "m4a"
    # Not in the backup, for no reason the database gives.
    assert memos[10]["audio_available"] is False
    assert memos[10]["audio_missing_reason"] == "not_in_backup"
    # Matched by unique id, case-insensitively.
    assert memos[4]["file_name"] == "20260220 120000-ab12cd34.m4a" and memos[4]["audio_available"] is True
    # A full device path in ZPATH.
    assert memos[5]["file_name"] == "20250101 101010-55556666.m4a" and memos[5]["audio_available"] is True
    # QuickTime formats.
    assert (memos[6]["format"], memos[7]["format"]) == ("qta", "qt")
    # Nothing to go on.
    assert memos[11]["file_name"] is None and memos[11]["format"] is None
    assert memos[11]["audio_missing_reason"] == "not_in_backup"


def test_every_flag(library):
    memos = _by_id(VoiceMemoExtractor().list_voice_memos(library))
    assert memos[6]["is_layered"] is True and memos[6]["is_spatial"] is False
    assert memos[7]["is_spatial"] is True and memos[7]["is_layered"] is False
    assert memos[8]["recorded_on_watch"] is True and memos[8]["is_enhanced"] is True
    assert memos[9]["recorded_on_watch"] is True and memos[9]["is_enhanced"] is False
    assert memos[1]["has_transcript"] is True and memos[2]["has_transcript"] is False
    assert memos[1]["is_favorite"] is True and memos[2]["is_favorite"] is False
    # 0x2 alone still counts as being in the phone's list: (ZFLAGS & 6) != 0.
    assert memos[3]["in_phone_list"] is True
    # NULL flags in a table that has the column.
    blank = memos[11]
    assert blank["in_phone_list"] is False and blank["has_transcript"] is False
    assert blank["is_favorite"] is False and blank["recorded_on_watch"] is False
    assert blank["title"] is None and blank["date"] is None and blank["duration_seconds"] is None


@pytest.mark.parametrize("flags, shared, expected", [
    (0x0, 0x0, {"in_phone_list": False}),
    (0x2, 0x0, {"in_phone_list": True}),
    (0x4, 0x0, {"in_phone_list": True}),
    (0x40, 0x0, {"recorded_on_watch": True}),
    (0x0, 0x40, {"recorded_on_watch": True}),
    (0x400, 0x0, {"has_transcript": True}),
    (0x200, 0x0, {"has_transcript": False}),  # transcribed, but no text
    (0x800, 0x0, {"is_layered": True}),
    (0x1000, 0x0, {"is_spatial": True}),
    (0x0, 0x1, {"is_favorite": True}),
    (0x0, 0x2, {"is_enhanced": True}),
    (0x0, 0x4, {"is_favorite": False, "is_enhanced": False}),  # a Music Memo
    (0x7FFF, 0x7F, {"in_phone_list": True, "recorded_on_watch": True, "has_transcript": True,
                    "is_layered": True, "is_spatial": True, "is_favorite": True, "is_enhanced": True}),
])
def test_flag_bits(tmp_path, flags, shared, expected):
    db = _store(_db_file(tmp_path), recordings=[{"Z_PK": 1, "ZDATE": 1.0, "ZFLAGS": flags, "ZSHAREDFLAGS": shared}])
    memo = VoiceMemoExtractor().list_voice_memos(
        _FakeBackup({(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): db}))["voice_memos"][0]
    defaults = {"in_phone_list": False, "recorded_on_watch": False, "has_transcript": False,
                "is_layered": False, "is_spatial": False, "is_favorite": False, "is_enhanced": False}
    assert {k: memo[k] for k in defaults} == {**defaults, **expected}
    assert memo["audio_missing_reason"] == ("in_icloud_only" if flags & 0x2 else "not_in_backup")


TITLE_COLUMNS = (
    "Z_PK INTEGER PRIMARY KEY, ZDATE TIMESTAMP, ZCUSTOMLABEL VARCHAR, ZCUSTOMLABELFORSORTING VARCHAR,"
    " ZENCRYPTEDTITLE VARCHAR, ZENCRYPTEDCUSTOMLABEL VARCHAR"
)


@pytest.mark.parametrize("row, title", [
    ({"ZCUSTOMLABELFORSORTING": "Sorting", "ZENCRYPTEDTITLE": "Encrypted",
      "ZENCRYPTEDCUSTOMLABEL": "Encrypted label", "ZCUSTOMLABEL": "Label"}, "Sorting"),
    ({"ZCUSTOMLABELFORSORTING": "  ", "ZENCRYPTEDTITLE": "Encrypted", "ZCUSTOMLABEL": "Label"}, "Encrypted"),
    ({"ZCUSTOMLABELFORSORTING": "", "ZENCRYPTEDCUSTOMLABEL": "Encrypted label", "ZCUSTOMLABEL": "Label"},
     "Encrypted label"),
    ({"ZCUSTOMLABEL": "  Label  "}, "Label"),
    ({}, None),
    # iOS 17: ZCUSTOMLABEL holds a date string for older devices; the real title is elsewhere.
    ({"ZCUSTOMLABEL": "May 13, 2026 at 2:15 PM", "ZENCRYPTEDTITLE": "Kitchen tiles",
      "ZCUSTOMLABELFORSORTING": "Kitchen tiles"}, "Kitchen tiles"),
    ({"ZCUSTOMLABEL": "May 13, 2026 at 2:15 PM", "ZENCRYPTEDTITLE": "Kitchen tiles"}, "Kitchen tiles"),
])
def test_title_precedence(tmp_path, row, title):
    db = _store(_db_file(tmp_path), TITLE_COLUMNS, [{"Z_PK": 1, "ZDATE": 1.0, **row}], folder_cols=None,
                other_tables=())
    memo = VoiceMemoExtractor().list_voice_memos(
        _FakeBackup({(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): db}))["voice_memos"][0]
    assert memo["title"] == title
    # A table without the flag columns reads them as unknown.
    assert memo["is_favorite"] is None and memo["has_transcript"] is None and memo["in_phone_list"] is None
    assert memo["recorded_on_watch"] is None and memo["is_enhanced"] is None


def test_duration_prefers_local_duration_above_zero(tmp_path):
    db = _store(_db_file(tmp_path), recordings=[
        {"Z_PK": 1, "ZDATE": 3.0, "ZDURATION": 10.0, "ZLOCALDURATION": 10.25},
        {"Z_PK": 2, "ZDATE": 2.0, "ZDURATION": 10.0, "ZLOCALDURATION": 0.0},
        {"Z_PK": 3, "ZDATE": 1.0, "ZDURATION": 0.0, "ZLOCALDURATION": None},
        {"Z_PK": 4, "ZDATE": 0.5, "ZDURATION": "not a number", "ZLOCALDURATION": None},
    ])
    memos = _by_id(VoiceMemoExtractor().list_voice_memos(_FakeBackup({(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): db})))
    assert [memos[i]["duration_seconds"] for i in (1, 2, 3, 4)] == [10.25, 10.0, 0.0, None]


def test_same_date_newest_id_first_and_undated_last(tmp_path):
    when = _apple("2026-05-01T09:00:00")
    db = _store(_db_file(tmp_path), recordings=[
        {"Z_PK": 1, "ZDATE": None}, {"Z_PK": 2, "ZDATE": when}, {"Z_PK": 3, "ZDATE": when},
        {"Z_PK": 4, "ZDATE": when + 1}, {"Z_PK": 5, "ZDATE": "garbage"},
    ])
    result = VoiceMemoExtractor().list_voice_memos(_FakeBackup({(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): db}))
    assert [m["id"] for m in result["voice_memos"]] == [4, 3, 2, 5, 1]


def test_path_matching_details(tmp_path):
    db = _store(_db_file(tmp_path), recordings=[
        # ZPATH in another case than the file in the manifest.
        {"Z_PK": 1, "ZDATE": 3.0, "ZUNIQUEID": "11110000-0000", "ZPATH": "20260101 120000-11110000.M4A"},
        # ZPATH names a file that's gone, but the standard name of its unique id is there.
        {"Z_PK": 2, "ZDATE": 2.0, "ZUNIQUEID": "22220000-0000", "ZPATH": "Renamed copy.m4a"},
        # A unique id too short to match anything.
        {"Z_PK": 3, "ZDATE": 1.0, "ZUNIQUEID": "333", "ZPATH": None},
    ])
    backup = _FakeBackup({
        (VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): db,
        (VOICE_MEMOS_DOMAIN, "Recordings/20260101 120000-11110000.m4a"): _audio(tmp_path, "a.m4a"),
        (VOICE_MEMOS_DOMAIN, "Recordings/20260102 120000-22220000.m4a"): _audio(tmp_path, "b.m4a"),
        (VOICE_MEMOS_DOMAIN, "Recordings/20260102 120000-22220000.qta"): _audio(tmp_path, "c.qta"),
        (VOICE_MEMOS_DOMAIN, "Recordings/333.m4a"): _audio(tmp_path, "d.m4a"),
    })
    memos = _by_id(VoiceMemoExtractor().list_voice_memos(backup))
    assert memos[1]["file_name"] == "20260101 120000-11110000.m4a" and memos[1]["audio_available"]
    # Both an .m4a and a .qta carry its id: the .m4a is taken.
    assert memos[2]["file_name"] == "20260102 120000-22220000.m4a" and memos[2]["audio_available"]
    assert memos[3]["file_name"] is None and not memos[3]["audio_available"]


def test_invalid_utf8_and_blob_titles(tmp_path):
    db = _store(_db_file(tmp_path), recordings=[
        {"Z_PK": 1, "ZDATE": 2.0}, {"Z_PK": 2, "ZDATE": 1.0},
        {"Z_PK": 3, "ZDATE": 0.5, "ZENCRYPTEDTITLE": "Café".encode()},
    ], folders=[{"Z_PK": 1, "ZUUID": "F", "ZRANK": 0}])
    conn = sqlite3.connect(db)
    # A 4-byte sequence cut in half, stored as TEXT; and raw bytes stored as a BLOB.
    conn.execute("UPDATE ZCLOUDRECORDING SET ZCUSTOMLABELFORSORTING = CAST(X'4869F09F' AS TEXT), ZFOLDER = 1 WHERE Z_PK = 1")
    conn.execute("UPDATE ZCLOUDRECORDING SET ZENCRYPTEDTITLE = X'4F6Bff21' WHERE Z_PK = 2")
    conn.execute("UPDATE ZFOLDER SET ZENCRYPTEDNAME = CAST(X'4170EDA0BD' AS TEXT) WHERE Z_PK = 1")
    conn.commit()
    conn.close()
    result = VoiceMemoExtractor().list_voice_memos(_FakeBackup({(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): db}))
    assert "error" not in result
    memos = _by_id(result)
    assert memos[1]["title"].startswith("Hi")
    assert memos[2]["title"] == "Ok�!"
    assert memos[3]["title"] == "Café"
    assert result["folders"][0]["name"].startswith("Ap") and memos[1]["folder"] == result["folders"][0]["name"]


# ---------------------------------------------------------------------------
# Model 6 (iOS 13), from the harvested schema
# ---------------------------------------------------------------------------

MODEL6_ROWS = [
    {"Z_PK": 1, "Z_ENT": 1, "Z_OPT": 1, "ZUNIQUEID": "C0FFEE01-AAAA-4BBB-8CCC-DDDDEEEEFFFF",
     "ZDATE": _apple("2020-03-04T15:00:00"), "ZDURATION": 14.0, "ZCUSTOMLABEL": "Lecture notes",
     "ZPATH": "20200304 070000-C0FFEE01.m4a", "ZFLAGS": 0x4 | 0x40},
    {"Z_PK": 2, "Z_ENT": 1, "Z_OPT": 1, "ZUNIQUEID": "C0FFEE02-AAAA-4BBB-8CCC-DDDDEEEEFFFF",
     "ZDATE": _apple("2020-03-05T15:00:00"), "ZDURATION": 3.5, "ZCUSTOMLABEL": "Parking spot",
     "ZPATH": "/var/mobile/Media/Recordings/20200305 070000-C0FFEE02.m4a", "ZFLAGS": 0x4,
     "ZEVICTIONDATE": _apple("2020-03-06T00:00:00")},
]


def test_model_6_from_the_harvested_ios13_schema(tmp_path):
    db = _harvested(_db_file(tmp_path), rows=MODEL6_ROWS)
    backup = _FakeBackup({
        (MEDIA_DOMAIN, MEDIA_DB_PATH): db,
        (MEDIA_DOMAIN, "Media/Recordings/20200304 070000-C0FFEE01.m4a"): _audio(tmp_path, "m6.m4a"),
    })
    result = VoiceMemoExtractor().list_voice_memos(backup)
    assert result["folders"] == []
    memos = result["voice_memos"]
    assert [m["id"] for m in memos] == [2, 1]
    lecture, parking = memos[1], memos[0]
    assert lecture["title"] == "Lecture notes" and lecture["duration_seconds"] == 14.0
    assert lecture["recorded_on_watch"] is True and lecture["in_phone_list"] is True
    # Model 6 has ZFLAGS but no ZSHAREDFLAGS: favourites and enhancement are unknown.
    assert lecture["is_favorite"] is None and lecture["is_enhanced"] is None
    assert lecture["has_transcript"] is False and lecture["is_layered"] is False
    assert lecture["audio_available"] is True and lecture["source"] == "CloudRecordings.db"
    assert parking["is_deleted"] is True and parking["deleted_date"] == "2020-03-06T00:00:00+00:00"
    assert parking["file_name"] == "20200305 070000-C0FFEE02.m4a" and parking["audio_available"] is False


def test_empty_ios13_databases_like_the_corpus(tmp_path):
    # iOS 13.3.1 and 13.4.1: both databases present, no rows, no audio.
    backup = _FakeBackup({
        (MEDIA_DOMAIN, MEDIA_DB_PATH): _harvested(_db_file(tmp_path)),
        (MEDIA_DOMAIN, "Media/Recordings/Recordings.db"): _harvested(_db_file(tmp_path, "Recordings.db")),
    })
    assert VoiceMemoExtractor().list_voice_memos(backup) == {"voice_memos": [], "empty_reason": "no_records"}


# ---------------------------------------------------------------------------
# Finding the database
# ---------------------------------------------------------------------------

def _one(tmp_path, title, **row):
    return _store(_db_file(tmp_path), recordings=[{"Z_PK": 1, "ZDATE": 1.0, "ZENCRYPTEDTITLE": title, **row}])


def test_prefers_the_voice_memos_app_group_over_media_domain(tmp_path):
    backup = _FakeBackup({
        (MEDIA_DOMAIN, MEDIA_DB_PATH): _one(tmp_path, "Old place"),
        (VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): _one(tmp_path, "App group"),
        ("AppDomain-com.example.recorder", "Documents/CloudRecordings.db"): _one(tmp_path, "Elsewhere"),
    })
    assert VoiceMemoExtractor().list_voice_memos(backup)["voice_memos"][0]["title"] == "App group"


def test_prefers_media_domain_over_other_domains(tmp_path):
    backup = _FakeBackup({
        ("AppDomain-com.example.recorder", "Documents/CloudRecordings.db"): _one(tmp_path, "Elsewhere"),
        (MEDIA_DOMAIN, MEDIA_DB_PATH): _one(tmp_path, "Media"),
    })
    assert VoiceMemoExtractor().list_voice_memos(backup)["voice_memos"][0]["title"] == "Media"


def test_any_other_domain_with_audio_beside_it(tmp_path):
    domain = "AppDomainGroup-group.com.example.voicememos"
    backup = _FakeBackup({
        (domain, "Library/Recs/CloudRecordings.db"): _one(tmp_path, "Found", ZPATH="20260101 000000-0A0B0C0D.m4a"),
        (domain, "Library/Recs/20260101 000000-0A0B0C0D.m4a"): _audio(tmp_path, "other.m4a"),
        (VOICE_MEMOS_DOMAIN, "Recordings/20260101 000000-0A0B0C0D.m4a"): _audio(tmp_path, "decoy.m4a"),
    })
    ex = VoiceMemoExtractor()
    memo = ex.list_voice_memos(backup)["voice_memos"][0]
    assert memo["title"] == "Found" and memo["audio_available"] is True
    assert ex.get_audio_path(backup, 1) == backup.files[(domain, "Library/Recs/20260101 000000-0A0B0C0D.m4a")]


def test_a_database_inside_a_composition_folder_is_ignored(tmp_path):
    backup = _FakeBackup({
        (VOICE_MEMOS_DOMAIN, "Recordings/20260101 000000-0A0B0C0D.composition/CloudRecordings.db"):
            _one(tmp_path, "Not the index"),
    })
    assert VoiceMemoExtractor().list_voice_memos(backup) == {"voice_memos": [], "empty_reason": "not_in_backup"}


LEGACY_ROWS = [
    {"Z_PK": 1, "ZUNIQUEID": "1E6AC701-AAAA-4BBB-8CCC-DDDDEEEEFFFF", "ZDATE": _apple("2017-08-01T12:00:00"),
     "ZDURATION": 20.0, "ZCUSTOMLABEL": "Before the update", "ZPATH": "/var/mobile/Media/Recordings/20170801 050000.m4a"},
    {"Z_PK": 2, "ZUNIQUEID": None, "ZDATE": _apple("2017-07-01T12:00:00"), "ZDURATION": 4.0,
     "ZCUSTOMLABEL": None, "ZPATH": "20170701 050000.m4a"},
]


def _legacy(tmp_path):
    return _harvested(_db_file(tmp_path, "Recordings.db"), table="ZRECORDING", rows=LEGACY_ROWS)


def test_legacy_recordings_db_when_there_is_no_cloud_database(tmp_path):
    backup = _FakeBackup({
        (MEDIA_DOMAIN, "Media/Recordings/Recordings.db"): _legacy(tmp_path),
        (MEDIA_DOMAIN, "Media/Recordings/20170801 050000.m4a"): _audio(tmp_path, "legacy.m4a"),
    })
    result = VoiceMemoExtractor().list_voice_memos(backup)
    assert result["folders"] == []
    first, second = result["voice_memos"]
    assert first == {
        "id": 1, "unique_id": "1E6AC701-AAAA-4BBB-8CCC-DDDDEEEEFFFF", "title": "Before the update",
        "date": "2017-08-01T12:00:00+00:00", "duration_seconds": 20.0, "folder_id": None, "folder": None,
        "is_favorite": None, "is_deleted": False, "deleted_date": None, "in_phone_list": None,
        "recorded_on_watch": None, "is_enhanced": None, "is_layered": None, "is_spatial": None,
        "has_transcript": None, "file_name": "20170801 050000.m4a", "format": "m4a",
        "audio_available": True, "audio_missing_reason": None, "source": "Recordings.db",
    }
    assert second["title"] is None and second["audio_missing_reason"] == "not_in_backup"
    ex = VoiceMemoExtractor()
    assert ex.get_audio_path(backup, 1) == backup.files[(MEDIA_DOMAIN, "Media/Recordings/20170801 050000.m4a")]


def test_legacy_result_has_no_folders_even_when_the_file_has_some(tmp_path):
    # iOS 12 and 13 write the whole model into Recordings.db too; its folders
    # belong to ZCLOUDRECORDING, not to the older ZRECORDING rows.
    path = _store(_db_file(tmp_path, "Recordings.db"), folders=FOLDERS)
    conn = sqlite3.connect(path)
    for row in LEGACY_ROWS:
        _insert(conn, "ZRECORDING", row)
    conn.commit()
    conn.close()
    result = VoiceMemoExtractor().list_voice_memos(_FakeBackup({(MEDIA_DOMAIN, "Media/Recordings/Recordings.db"): path}))
    assert result["folders"] == []
    assert [m["source"] for m in result["voice_memos"]] == ["Recordings.db"] * 2


def test_legacy_recordings_db_beside_an_empty_cloud_database(tmp_path):
    backup = _FakeBackup({
        (MEDIA_DOMAIN, MEDIA_DB_PATH): _harvested(_db_file(tmp_path)),
        (MEDIA_DOMAIN, "Media/Recordings/Recordings.db"): _legacy(tmp_path),
    })
    result = VoiceMemoExtractor().list_voice_memos(backup)
    assert {m["source"] for m in result["voice_memos"]} == {"Recordings.db"}
    assert len(result["voice_memos"]) == 2


def test_one_source_only_when_the_cloud_database_has_recordings(tmp_path):
    backup = _FakeBackup({
        (MEDIA_DOMAIN, MEDIA_DB_PATH): _harvested(_db_file(tmp_path), rows=MODEL6_ROWS),
        (MEDIA_DOMAIN, "Media/Recordings/Recordings.db"): _legacy(tmp_path),
    })
    result = VoiceMemoExtractor().list_voice_memos(backup)
    assert [m["source"] for m in result["voice_memos"]] == ["CloudRecordings.db"] * 2


def test_legacy_database_in_another_folder_is_not_used(tmp_path):
    backup = _FakeBackup({
        (VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): _store(_db_file(tmp_path)),
        (MEDIA_DOMAIN, "Media/Recordings/Recordings.db"): _legacy(tmp_path),
    })
    assert VoiceMemoExtractor().list_voice_memos(backup) == {"voice_memos": [], "empty_reason": "no_records"}


def test_unreadable_legacy_database_beside_an_empty_cloud_database_is_an_error(tmp_path):
    backup = _FakeBackup({(MEDIA_DOMAIN, MEDIA_DB_PATH): _harvested(_db_file(tmp_path))},
                         unreadable={(MEDIA_DOMAIN, "Media/Recordings/Recordings.db")})
    result = VoiceMemoExtractor().list_voice_memos(backup)
    assert result["voice_memos"] == [] and "empty_reason" not in result
    assert result["error"].startswith(NOT_READABLE)


# ---------------------------------------------------------------------------
# Empty and error states
# ---------------------------------------------------------------------------

def test_not_in_backup():
    assert VoiceMemoExtractor().list_voice_memos(_FakeBackup()) == {
        "voice_memos": [], "empty_reason": "not_in_backup"}


def test_empty_library(tmp_path):
    backup = _FakeBackup({(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): _store(_db_file(tmp_path), folders=FOLDERS)})
    assert VoiceMemoExtractor().list_voice_memos(backup) == {"voice_memos": [], "empty_reason": "no_records"}


@pytest.mark.parametrize("tables", [
    (),                                                               # no tables at all
    ("CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY)",
     "CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY)"),         # only Core Data's own
    ("CREATE TABLE ZRECORDING (Z_PK INTEGER PRIMARY KEY, ZDATE TIMESTAMP)",),  # the other recordings table
])
def test_database_without_recordings_is_no_records(tmp_path, tables):
    path = _db_file(tmp_path)
    conn = sqlite3.connect(path)
    for ddl in tables:
        conn.execute(ddl)
    conn.commit()
    conn.close()
    backup = _FakeBackup({(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): str(path)})
    assert VoiceMemoExtractor().list_voice_memos(backup) == {"voice_memos": [], "empty_reason": "no_records"}


def test_unknown_layout_is_unsupported(tmp_path):
    path = _db_file(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ZRECORDINGITEM (Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR)")
    conn.execute("CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    backup = _FakeBackup({(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): str(path)})
    assert VoiceMemoExtractor().list_voice_memos(backup) == {
        "voice_memos": [], "error": UNSUPPORTED, "empty_reason": "unsupported_format"}


def test_listed_but_unreadable_is_an_error():
    backup = _FakeBackup(unreadable={(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH)})
    result = VoiceMemoExtractor().list_voice_memos(backup)
    assert result == {"voice_memos": [], "error": NOT_READABLE + " The backup may be incomplete or damaged."}


def test_corrupt_file_is_an_error(tmp_path):
    path = _db_file(tmp_path)
    path.write_bytes(b"not a database" * 100)
    result = VoiceMemoExtractor().list_voice_memos(_FakeBackup({(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): str(path)}))
    assert result["voice_memos"] == [] and "empty_reason" not in result
    assert result["error"].startswith(NOT_READABLE) and result["error"].endswith(")")


def test_damage_found_while_reading_rows_is_an_error(tmp_path, capsys):
    path = _db_file(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size = 1024")
    conn.execute(f"CREATE TABLE ZCLOUDRECORDING ({MODEL14})")
    for pk in range(1, 300):
        _insert(conn, "ZCLOUDRECORDING", {"Z_PK": pk, "ZDATE": float(pk), "ZENCRYPTEDTITLE": "x" * 200})
    conn.commit()
    conn.close()
    data = bytearray(path.read_bytes())
    data[1024 * 5:1024 * 9] = b"\xff" * 4096  # pages 6-9: table data, not the schema on page 1
    path.write_bytes(bytes(data))
    result = VoiceMemoExtractor().list_voice_memos(_FakeBackup({(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH): str(path)}))
    assert result["voice_memos"] == [] and "empty_reason" not in result
    assert result["error"].startswith(NOT_READABLE + " The backup may be incomplete or damaged. (")
    assert "[voice_memos]" in capsys.readouterr().err


def test_a_failure_while_building_records_is_an_error(library, monkeypatch):
    def broken(self, backup, loaded):
        raise RuntimeError("boom")
    monkeypatch.setattr(VoiceMemoExtractor, "_memos", broken)
    result = VoiceMemoExtractor().list_voice_memos(library)
    assert result == {"voice_memos": [], "error": NOT_READABLE + " The backup may be incomplete or damaged. (boom)"}
    assert VoiceMemoExtractor().get_audio_path(library, 1) is None


def test_a_failing_manifest_query_lists_audio_as_missing(library, monkeypatch):
    real = library.list_files

    def flaky(domain=None, path_like=None):
        if path_like and path_like.endswith("/%"):
            raise OSError("manifest went away")
        return real(domain=domain, path_like=path_like)
    monkeypatch.setattr(library, "list_files", flaky)
    memos = VoiceMemoExtractor().list_voice_memos(library)["voice_memos"]
    assert memos and not any(m["audio_available"] for m in memos)


# ---------------------------------------------------------------------------
# Audio and transcripts
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS = (".m4a", ".qta", ".qt")


def test_listing_never_opens_audio(library):
    VoiceMemoExtractor().list_voice_memos(library)
    assert library.fetched, "the database itself is fetched"
    assert not [p for _d, p in library.fetched if p.lower().endswith(AUDIO_EXTENSIONS)]


def test_get_audio_path(library):
    ex = VoiceMemoExtractor()
    expected = library.files[(VOICE_MEMOS_DOMAIN, f"{FOLDER}/{FILE_WALK}")]
    assert ex.get_audio_path(library, 1) == expected
    assert ex.get_audio_path(library, "1") == expected
    # Found by unique id and by a full device path.
    assert ex.get_audio_path(library, 4) == library.files[(VOICE_MEMOS_DOMAIN, f"{FOLDER}/20260220 120000-ab12cd34.m4a")]
    assert ex.get_audio_path(library, 5) == library.files[(VOICE_MEMOS_DOMAIN, f"{FOLDER}/20250101 101010-55556666.m4a")]
    # Deleted recordings still have their audio.
    assert ex.get_audio_path(library, 2) is not None
    # Not in the backup, only in iCloud, nothing to go on, or no such recording.
    library.fetched.clear()
    for memo_id in (10, 3, 11, 999):
        assert ex.get_audio_path(library, memo_id) is None
    # A file the manifest doesn't list is never asked for.
    assert not [p for _d, p in library.fetched if p.lower().endswith(AUDIO_EXTENSIONS)]


@pytest.mark.parametrize("memo_id", [True, False, -1, "-1", "../../x", None, "12abc", "", " 1", "1 ", "1.0",
                                     1.0, "١", 2**63, "9" * 5000, [1], b"1"])
def test_ids_that_are_not_ids(library, memo_id):
    ex = VoiceMemoExtractor()
    library.fetched.clear()
    assert ex.get_audio_path(library, memo_id) is None
    assert ex.get_transcript(library, memo_id) is None
    assert library.fetched == []  # rejected before the backup is touched


def test_audio_path_must_exist(library, tmp_path):
    library.files[(VOICE_MEMOS_DOMAIN, f"{FOLDER}/{FILE_WALK}")] = str(tmp_path / "gone.m4a")
    assert VoiceMemoExtractor().get_audio_path(library, 1) is None


def test_get_audio_path_when_the_library_is_missing_or_unreadable():
    ex = VoiceMemoExtractor()
    assert ex.get_audio_path(_FakeBackup(), 1) is None
    assert ex.get_audio_path(_FakeBackup(unreadable={(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH)}), 1) is None


def test_get_transcript(library):
    ex = VoiceMemoExtractor()
    assert ex.get_transcript(library, 1) == {
        "text": "Check the north stairwell.",
        "segments": [
            {"text": "Check", "start": 0.0, "end": 0.8},
            {"text": " the north", "start": 0.8, "end": 1.5},
            {"text": " stairwell.", "start": 1.5, "end": 2.2},
        ],
        "locale": "en_US",
    }
    # Audio without a transcript; no audio at all; an unknown recording.
    assert ex.get_transcript(library, 4) is None
    assert ex.get_transcript(library, 3) is None
    assert ex.get_transcript(library, 999) is None


def test_get_transcript_of_a_damaged_file(library, tmp_path):
    library.files[(VOICE_MEMOS_DOMAIN, f"{FOLDER}/{FILE_WALK}")] = _audio(tmp_path, "damaged.m4a", b"\x00\x00\x00\x18moov" + b"\xff" * 64)
    assert VoiceMemoExtractor().get_transcript(library, 1) is None


# ---------------------------------------------------------------------------
# A real backup, unencrypted and encrypted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("password", [None, "pw"])
def test_reads_a_real_backup(tmp_path, password, monkeypatch):
    db = _store(_db_file(tmp_path), recordings=RECORDINGS[:4], folders=FOLDERS)
    device = Device(name="Test iPhone", product_type="iPhone17,1", product_name="iPhone 16 Pro",
                    ios="26.5", build="23F77", phone_number="+44 7700 900001")
    writer = BackupWriter(tmp_path / "out", device, datetime(2026, 6, 1, tzinfo=timezone.utc),
                          "17.3", password=password, seed=1)
    writer.add_file(VOICE_MEMOS_DOMAIN, VOICE_MEMOS_DB_PATH, Path(db).read_bytes())
    writer.add_file(VOICE_MEMOS_DOMAIN, f"{FOLDER}/{FILE_WALK}", _m4a(WALK_TRANSCRIPT))
    writer.add_file(VOICE_MEMOS_DOMAIN, f"{FOLDER}/20260402 010000-22223333.m4a", _m4a())
    writer.add_file(VOICE_MEMOS_DOMAIN, f"{FOLDER}/20260513 141522-6F3B2C1A.composition/manifest.plist", b"<plist/>")
    backup_dir = writer.finalize()
    accessor = open_local_backup(str(backup_dir), password=password)

    fetched = []
    real_get_file = accessor.get_file

    def spy(relative_path, domain="HomeDomain"):
        fetched.append(relative_path)
        return real_get_file(relative_path, domain)
    monkeypatch.setattr(accessor, "get_file", spy)

    ex = VoiceMemoExtractor()
    result = ex.list_voice_memos(accessor)
    memos = _by_id(result)
    assert [m["id"] for m in result["voice_memos"]] == [1, 2, 3, 4]
    assert [memos[i]["audio_available"] for i in (1, 2, 3, 4)] == [True, True, False, False]
    assert memos[3]["audio_missing_reason"] == "in_icloud_only"
    assert result["folders"][2] == {"id": "F0A1-SITE", "name": "Site visits", "rank": 1, "count": 1}
    assert not [p for p in fetched if p.lower().endswith(AUDIO_EXTENSIONS)]

    path = ex.get_audio_path(accessor, 1)
    assert path and os.path.exists(path)
    if password is None:
        # Unencrypted: the file inside the backup, under its hash, with no extension.
        assert Path(path).resolve().is_relative_to(Path(backup_dir).resolve())
    assert ex.get_transcript(accessor, "1")["text"] == "Check the north stairwell."
    assert ex.get_transcript(accessor, 2) is None
    assert ex.get_audio_path(accessor, 3) is None
