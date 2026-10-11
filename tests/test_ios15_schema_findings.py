"""Regression tests for problems OpenExtract's corpus tests found in a real iOS 15.3.1 backup.

Each test builds the smallest database that reproduces the iOS 15 layout:
- CallHistory: all-digit addresses stored as INTEGER, no ZIS_VIDEO column, ZANSWERED = 0 on outgoing calls.
- NoteStore: a note's creation date in ZCREATIONDATE3.
- Safari: domain_expansion holding a short label ("nhl"), not a host.
- Photos.sqlite: a WAL database shipped without its -shm file, with ZMODIFICATIONDATE / ZAVALANCHEUUID
  in place of ZDATEMODIFIED / ZBURSTUUID.
"""

import fnmatch
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.contacts import clean_phone_number, resolve_contact
from ios_backup_core.extractors.browser_history import BrowserHistoryExtractor
from ios_backup_core.extractors.calls import CallExtractor
from ios_backup_core.extractors.messages import MessageExtractor
from ios_backup_core.extractors.notes import NoteExtractor
from ios_backup_core.extractors.photos import PhotoExtractor
from ios_backup_core.timestamps import apple_to_iso

MAY_2023 = 705_373_000.0  # seconds since 2001-01-01, 2023-05-09


class _FakeBackup:
    encrypted = False

    def __init__(self, files: dict):
        self.files = files  # {(domain, relative_path): local path}

    def get_file(self, relative_path, domain="HomeDomain"):
        return self.files.get((domain, relative_path))

    def list_files(self, domain=None, path_like=None):
        pattern = (path_like or "%").replace("%", "*")
        return [
            {"domain": d, "path": p, "hash": f"{d}-{p}"}
            for (d, p) in self.files
            if fnmatch.fnmatch(p, pattern) and (domain is None or d == domain)
        ]


def _db(path, script, rows=()):
    conn = sqlite3.connect(path)
    conn.executescript(script)
    for sql, params in rows:
        conn.execute(sql, params)
    conn.commit()
    conn.close()
    return str(path)


# ── calls ────────────────────────────────────────────────────────────────────


def _call_history(tmp_path):
    return _db(
        tmp_path / "CallHistory.storedata",
        """
        CREATE TABLE ZCALLRECORD (
            Z_PK INTEGER PRIMARY KEY, ZADDRESS, ZDATE TIMESTAMP, ZDURATION FLOAT,
            ZCALLTYPE INTEGER, ZORIGINATED INTEGER, ZANSWERED INTEGER, ZSERVICE_PROVIDER VARCHAR
        );
        """,
        [
            ("INSERT INTO ZCALLRECORD VALUES (?,?,?,?,?,?,?,?)", row)
            for row in [
                (1, 22000, MAY_2023, 0.0, 1, 0, 0, "com.apple.Telephony"),  # short code stored as INTEGER
                (2, "+15555550100", MAY_2023 + 60, 91.0, 8, 1, 0, "com.apple.FaceTime"),
                (3, "+15555550100", MAY_2023 + 300, 101.0, 16, 0, 1, "com.apple.FaceTime"),
                (4, "+15555550100", MAY_2023 + 600, 0.0, 1, 1, 0, "com.apple.Telephony"),
            ]
        ],
    )


def _calls(tmp_path):
    backup = _FakeBackup({("HomeDomain", "Library/CallHistoryDB/CallHistory.storedata"): _call_history(tmp_path)})
    result = CallExtractor().list_calls(backup, {"22000": "Bank alerts"})
    return {c["call_id"]: c for c in result["calls"]}


def test_integer_addresses_are_handled():
    assert clean_phone_number(22000) == "22000"
    assert resolve_contact(22000, {"22000": "Bank alerts"}) == "Bank alerts"


def test_integer_call_address_lists_and_resolves(tmp_path):
    call = _calls(tmp_path)[1]
    assert call["address"] == "22000"
    assert call["contact_name"] == "Bank alerts"


def test_facetime_video_comes_from_call_type_without_is_video(tmp_path):
    calls = _calls(tmp_path)
    assert calls[2]["app"] == "FaceTime Video"
    assert calls[3]["app"] == "FaceTime Audio"


def test_connected_outgoing_call_is_answered(tmp_path):
    calls = _calls(tmp_path)
    assert calls[2]["direction"] == "outgoing"
    assert calls[2]["status"] == "answered"  # ZANSWERED is 0, but the call lasted 91 s
    assert calls[3]["status"] == "answered"
    assert calls[4]["status"] == "missed"  # outgoing, never connected


# ── notes ────────────────────────────────────────────────────────────────────


def test_note_creation_date_from_zcreationdate3(tmp_path):
    path = _db(
        tmp_path / "NoteStore.sqlite",
        """
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (
            Z_PK INTEGER PRIMARY KEY, ZTITLE1 VARCHAR, ZCREATIONDATE TIMESTAMP, ZCREATIONDATE1 TIMESTAMP,
            ZCREATIONDATE3 TIMESTAMP, ZMODIFICATIONDATE1 TIMESTAMP, ZSNIPPET VARCHAR
        );
        CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZNOTE INTEGER, ZDATA BLOB);
        """,
        [("INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (8, 'Groceries', NULL, NULL, ?, ?, 'milk')", (MAY_2023, MAY_2023 + 30))],
    )
    backup = _FakeBackup({("AppDomainGroup-group.com.apple.notes", "NoteStore.sqlite"): path})
    (note,) = NoteExtractor().list_notes(backup)["notes"]
    assert note["created"] == apple_to_iso(MAY_2023)
    assert note["modified"] == apple_to_iso(MAY_2023 + 30)


# ── Safari ───────────────────────────────────────────────────────────────────


def test_safari_domain_is_the_url_host(tmp_path):
    path = _db(
        tmp_path / "History.db",
        """
        CREATE TABLE history_items (id INTEGER PRIMARY KEY, url TEXT, domain_expansion TEXT, visit_count INTEGER);
        CREATE TABLE history_visits (id INTEGER PRIMARY KEY, history_item INTEGER, visit_time REAL, title TEXT);
        INSERT INTO history_items VALUES (1, 'https://www.nhl.com/', 'nhl', 1);
        """,
        [("INSERT INTO history_visits VALUES (1, 1, ?, 'NHL.com')", (MAY_2023,))],
    )
    backup = _FakeBackup({("HomeDomain", "Library/Safari/History.db"): path})
    (visit,) = BrowserHistoryExtractor().list_browser_history(backup, "safari")["visits"]
    assert visit["domain"] == "www.nhl.com"


# ── Photos ───────────────────────────────────────────────────────────────────


def _photos_db(tmp_path):
    path = str(tmp_path / "Photos.sqlite")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE ZASSET (
            Z_PK INTEGER PRIMARY KEY, ZUUID VARCHAR, ZFILENAME VARCHAR, ZDIRECTORY VARCHAR, ZKIND INTEGER,
            ZDATECREATED TIMESTAMP, ZMODIFICATIONDATE TIMESTAMP, ZWIDTH INTEGER, ZHEIGHT INTEGER,
            ZDURATION FLOAT, ZFAVORITE INTEGER, ZHIDDEN INTEGER, ZHASADJUSTMENTS INTEGER,
            ZAVALANCHEUUID VARCHAR, ZLATITUDE FLOAT, ZLONGITUDE FLOAT, ZTRASHEDSTATE INTEGER
        );
        CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR, ZKIND INTEGER);
        INSERT INTO ZGENERICALBUM VALUES (1, 'Trip', 2);
        """
    )
    conn.execute(
        "INSERT INTO ZASSET VALUES (1, 'A-1', 'IMG_0001.HEIC', 'DCIM/100APPLE', 0, ?, ?, 4032, 3024, 0, 0, 0, 0, 'BURST-1', 35.6, -78.8, 0)",
        (MAY_2023, MAY_2023 + 5),
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    # A backup hands us the main file only. Some SQLite builds (macOS) keep
    # -wal/-shm after the last close, so remove them; the file stays in WAL mode.
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    with open(path, "rb") as f:
        assert f.read(20)[18] == 2, "fixture should be a WAL-mode database"
    return path


def test_photos_open_wal_database_without_shm(tmp_path):
    backup = _FakeBackup({("CameraRollDomain", "Media/PhotoData/Photos.sqlite"): _photos_db(tmp_path)})
    extractor = PhotoExtractor()
    albums = extractor.list_albums(backup)
    assert albums["source"] == "photos_sqlite", albums
    assert "Trip" in {a["title"] for a in albums["albums"]}
    result = extractor.list_photos(backup)
    assert result["source"] == "photos_sqlite", result
    assert not os.path.exists(backup.files[("CameraRollDomain", "Media/PhotoData/Photos.sqlite")] + "-shm")


def test_photos_before_ios14_use_zgenericasset(tmp_path):
    """iOS 13 and earlier call the assets table ZGENERICASSET (found on a real iOS 13.4.1 backup)."""
    path = _db(
        tmp_path / "Photos.sqlite",
        """
        CREATE TABLE ZGENERICASSET (
            Z_PK INTEGER PRIMARY KEY, ZUUID VARCHAR, ZFILENAME VARCHAR, ZDIRECTORY VARCHAR, ZKIND INTEGER,
            ZDATECREATED TIMESTAMP, ZMODIFICATIONDATE TIMESTAMP, ZTRASHEDSTATE INTEGER
        );
        CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR, ZKIND INTEGER);
        INSERT INTO ZGENERICALBUM VALUES (1, 'Trip', 2);
        """,
        [("INSERT INTO ZGENERICASSET VALUES (1, 'A-1', 'IMG_0001.JPG', 'DCIM/100APPLE', 0, ?, ?, 0)", (MAY_2023, MAY_2023))],
    )
    backup = _FakeBackup({("CameraRollDomain", "Media/PhotoData/Photos.sqlite"): path})
    result = PhotoExtractor().list_photos(backup)
    assert result["source"] == "photos_sqlite", result
    assert result["total"] == 1 and result["photos"][0]["date_created"] is not None
    albums = PhotoExtractor().list_albums(backup)
    assert albums["source"] == "photos_sqlite", albums
    assert albums["albums"][0]["asset_count"] == 1  # "All Photos"


def test_photos_read_renamed_asset_columns(tmp_path):
    backup = _FakeBackup({("CameraRollDomain", "Media/PhotoData/Photos.sqlite"): _photos_db(tmp_path)})
    (photo,) = PhotoExtractor().list_photos(backup)["photos"]
    assert photo["date_created"] is not None
    assert photo["date_modified"] is not None
    assert photo["burst_uuid"] == "BURST-1"
    assert photo["latitude"] == 35.6


# ── messages ─────────────────────────────────────────────────────────────────


def test_only_chat_number_identifiers_mark_a_group(tmp_path):
    """A 1:1 chat whose email handle contains "chat" isn't a group."""
    sms = _db(tmp_path / "sms.db", """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT, service_name TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, date INTEGER, is_from_me INTEGER,
                              handle_id INTEGER, service TEXT);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        INSERT INTO handle VALUES (1, 'chatham@example.com', 'iMessage'), (2, '+15555550100', 'iMessage');
        INSERT INTO chat VALUES (1, 'chatham@example.com', '', 'iMessage'), (2, 'chat644566139839284925', '', 'iMessage');
        INSERT INTO chat_handle_join VALUES (1, 1), (2, 2);
        INSERT INTO message VALUES (1, 'hi', 700000000000000000, 0, 1, 'iMessage'),
                                   (2, 'yo', 700000100000000000, 0, 2, 'iMessage');
        INSERT INTO chat_message_join VALUES (1, 1), (2, 2);
    """)
    result = MessageExtractor().list_conversations(_FakeBackup({("HomeDomain", "Library/SMS/sms.db"): sms}), {})
    kinds = {c["chat_id"]: c["is_group"] for c in result["conversations"]}
    assert kinds == {1: False, 2: True}
