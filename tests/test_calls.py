"""Tests for ios_backup_core.extractors.calls (openextract issues #57, #58, #59)."""

import os
import plistlib
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.calls import CallExtractor, _call_status, _clean_text  # noqa: E402


class FakeBackup:
    """Minimal BackupAccessor serving a single call-history DB."""

    def __init__(self, db_path):
        self.db_path = db_path

    def list_files(self, path_like=None):
        return [{"path": CallExtractor.CALL_HISTORY_PATH, "domain": "HomeDomain", "hash": "h1"}]

    def get_file(self, rel_path, domain="HomeDomain"):
        return self.db_path if rel_path == CallExtractor.CALL_HISTORY_PATH else None


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield os.path.join(tmp, "CallHistory.storedata")


def _make_zcallrecord_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE ZCALLRECORD (
            Z_PK INTEGER PRIMARY KEY, ZADDRESS, ZNAME, ZDATE REAL, ZDURATION REAL,
            ZCALLTYPE INTEGER, ZORIGINATED INTEGER, ZANSWERED INTEGER,
            ZSERVICE_PROVIDER TEXT, ZIS_VIDEO INTEGER
        )
    """)
    conn.executemany(
        "INSERT INTO ZCALLRECORD VALUES (?,?,?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()
    conn.close()


def _calls(db_path, contacts=None):
    result = CallExtractor().list_calls(FakeBackup(db_path), contacts or {})
    return {c["call_id"]: c for c in result["calls"]}


class TestCallStatus:
    def test_outgoing_with_duration_is_answered_even_if_zanswered_is_0(self):
        assert _call_status("outgoing", 0, 42.0) == "answered"

    def test_outgoing_without_duration_is_missed(self):
        assert _call_status("outgoing", 0, 0) == "missed"

    def test_incoming_answered_flag(self):
        assert _call_status("incoming", 1, 0) == "answered"

    def test_incoming_unanswered(self):
        assert _call_status("incoming", 0, 0) == "missed"

    def test_missing_values(self):
        assert _call_status("incoming", None, None) == "missed"


class TestCleanText:
    def test_strips_control_and_private_use(self):
        assert _clean_text("Alice\x00\x07 Smith\x1f") == "Alice Smith"

    def test_keeps_normal_unicode(self):
        assert _clean_text("José Ñúñez 👩‍💻") == "José Ñúñez 👩‍💻"

    def test_bytes_are_decoded(self):
        assert _clean_text(b"bob@example.com\x00\x01") == "bob@example.com"

    def test_binary_plist_string(self):
        assert _clean_text(plistlib.dumps("carol@corp.com", fmt=plistlib.FMT_BINARY)) == "carol@corp.com"

    def test_binary_plist_non_string_is_empty(self):
        assert _clean_text(plistlib.dumps({"a": 1}, fmt=plistlib.FMT_BINARY)) == ""

    def test_none(self):
        assert _clean_text(None) == ""


class TestListCalls:
    def test_outgoing_connected_call_not_marked_missed(self, db_path):
        # issue #59: ZANSWERED=0 on an outgoing call with a duration
        _make_zcallrecord_db(db_path, [
            (1, "+15551234567", None, 700000000.0, 125.0, 1, 1, 0, "com.apple.Telephony", 0),
            (2, "+15551234567", None, 700000100.0, 0.0, 1, 1, 0, "com.apple.Telephony", 0),
            (3, "+15551234567", None, 700000200.0, 0.0, 1, 0, 0, "com.apple.Telephony", 0),
        ])
        calls = _calls(db_path)
        assert (calls[1]["direction"], calls[1]["status"]) == ("outgoing", "answered")
        assert (calls[2]["direction"], calls[2]["status"]) == ("outgoing", "missed")
        assert (calls[3]["direction"], calls[3]["status"]) == ("incoming", "missed")

    def test_third_party_call_uses_clean_name(self, db_path):
        # issue #58: Teams (reported as Skype) records with garbage in ZADDRESS
        _make_zcallrecord_db(db_path, [
            (1, b"\x00\x02\xe2\x80\x8e8:orgid:1234\x07", "Dana\x00 Scully",
             700000000.0, 60.0, 1, 1, 1, "com.skype.skype", 0),
        ])
        call = _calls(db_path)[1]
        assert call["app"] == "Skype"
        assert call["contact_name"] == "Dana Scully"
        assert all(c.isprintable() for c in call["address"])

    def test_contact_match_still_wins_over_zname(self, db_path):
        _make_zcallrecord_db(db_path, [
            (1, "+15551234567", "Caller ID Name", 700000000.0, 10.0, 1, 0, 1, None, 0),
        ])
        call = _calls(db_path, {"+15551234567": "Alice"})[1]
        assert call["contact_name"] == "Alice"

    def test_international_number_matches_national_contact(self, db_path):
        # issue #57: contact saved as "06 12 34 56 78", call logged as +33…
        from ios_backup_core.contacts import add_phone_suffix_key
        contacts = {}
        add_phone_suffix_key(contacts, "06 12 34 56 78", "Émile")
        _make_zcallrecord_db(db_path, [
            (1, "+33612345678", None, 700000000.0, 10.0, 1, 0, 1, None, 0),
        ])
        assert _calls(db_path, contacts)[1]["contact_name"] == "Émile"

    def test_legacy_call_table_does_not_crash(self, db_path):
        # Old backups without ZCALLRECORD fall back to the `call` table, where
        # `originated` is NULL; this used to raise AttributeError (Row.get).
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE call (ROWID INTEGER PRIMARY KEY, address TEXT, date REAL,"
                     " duration REAL, flags INTEGER, read INTEGER)")
        conn.executemany("INSERT INTO call VALUES (?,?,?,?,?,?)", [
            (1, "5551234567", 700000000.0, 30.0, 5, 0),
            (2, "5551234567", 700000100.0, 0.0, 4, 0),
        ])
        conn.commit()
        conn.close()
        calls = _calls(db_path)
        assert (calls[1]["direction"], calls[1]["status"]) == ("outgoing", "answered")
        assert (calls[2]["direction"], calls[2]["status"]) == ("incoming", "missed")
