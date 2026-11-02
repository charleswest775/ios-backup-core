"""Tests for how CallExtractor and VoicemailExtractor report empty results.

An empty call or voicemail list is usually a normal state (a phone with no
calls, or no visual voicemail), not an error. Only an unencrypted backup with
no call history should carry the advice to turn on encryption.
"""

import hashlib
import os
import plistlib
import re
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.backup import open_local_backup
from ios_backup_core.extractors.calls import CallExtractor
from ios_backup_core.extractors.voicemail import VoicemailExtractor

CALL_DB = "Library/CallHistoryDB/CallHistory.storedata"
VOICEMAIL_DB = "Library/Voicemail/voicemail.db"


class _FakeBackup:
    """Accessor stub. ``files`` maps relative paths to local files;
    ``unreadable`` paths are listed in the manifest but can't be extracted."""

    def __init__(self, files=None, encrypted=False, unreadable=()):
        self.files = dict(files or {})
        self.encrypted = encrypted
        self.unreadable = set(unreadable)

    def get_file(self, relative_path, domain="HomeDomain"):
        return self.files.get(relative_path)

    def list_files(self, domain=None, path_like=None):
        paths = [*self.files, *self.unreadable]
        if path_like:
            pattern = ".*".join(re.escape(p) for p in path_like.split("%"))
            paths = [p for p in paths if re.fullmatch(pattern, p, re.IGNORECASE)]
        return [{"hash": "", "domain": "HomeDomain", "path": p} for p in paths]


def _call_db(path, rows=()):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE ZCALLRECORD (
            Z_PK INTEGER PRIMARY KEY, ZADDRESS TEXT, ZDATE REAL, ZDURATION REAL,
            ZCALLTYPE INTEGER, ZORIGINATED INTEGER, ZANSWERED INTEGER,
            ZSERVICE_PROVIDER TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO ZCALLRECORD VALUES (?, ?, ?, ?, 1, 0, 1, 'com.apple.Telephony')",
        rows,
    )
    conn.commit()
    conn.close()
    return str(path)


def _voicemail_db(path, rows=()):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE voicemail (
            ROWID INTEGER PRIMARY KEY, sender TEXT, date INTEGER, duration INTEGER,
            flags INTEGER, trashed_date INTEGER, token TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO voicemail VALUES (?, ?, ?, ?, 0, 0, '')", rows,
    )
    conn.commit()
    conn.close()
    return str(path)


def _list_calls(backup):
    return CallExtractor().list_calls(backup, contacts={})


def _list_voicemails(backup):
    return VoicemailExtractor().list_voicemails(backup, contacts={})


class TestCallsEmptyStates:
    def test_unencrypted_without_call_history_advises_encryption(self):
        result = _list_calls(_FakeBackup(encrypted=False))
        assert result["calls"] == []
        assert result["empty_reason"] == "requires_encryption"
        assert "encrypt" in result["error"].lower()

    def test_encrypted_without_call_history_is_not_an_error(self):
        result = _list_calls(_FakeBackup(encrypted=True))
        assert result["calls"] == []
        assert result["total"] == 0
        assert result["empty_reason"] == "not_in_backup"
        assert "error" not in result

    def test_encrypted_with_empty_call_history(self, tmp_path):
        backup = _FakeBackup({CALL_DB: _call_db(tmp_path / "calls.db")}, encrypted=True)
        result = _list_calls(backup)
        assert result["calls"] == []
        assert result["empty_reason"] == "no_records"
        assert "error" not in result

    def test_unencrypted_with_empty_call_history_does_not_advise_encryption(self, tmp_path):
        backup = _FakeBackup({CALL_DB: _call_db(tmp_path / "calls.db")}, encrypted=False)
        result = _list_calls(backup)
        assert result["empty_reason"] == "no_records"
        assert "error" not in result

    def test_listed_but_unreadable_call_history_is_an_error(self):
        result = _list_calls(_FakeBackup(encrypted=True, unreadable=[CALL_DB]))
        assert result["calls"] == []
        assert "could not be read" in result["error"]
        assert "empty_reason" not in result

    def test_calls_present_have_no_empty_reason(self, tmp_path):
        db = _call_db(tmp_path / "calls.db", rows=[(1, "+15551234567", 700000000.0, 42.0)])
        result = _list_calls(_FakeBackup({CALL_DB: db}, encrypted=True))
        assert len(result["calls"]) == 1
        assert result["total"] == 1
        assert "empty_reason" not in result
        assert "error" not in result


class TestVoicemailEmptyStates:
    def test_missing_voicemail_db_is_not_an_error(self):
        result = _list_voicemails(_FakeBackup())
        assert result["voicemails"] == []
        assert result["empty_reason"] == "not_in_backup"
        assert "error" not in result

    def test_empty_voicemail_db(self, tmp_path):
        backup = _FakeBackup({VOICEMAIL_DB: _voicemail_db(tmp_path / "vm.db")})
        result = _list_voicemails(backup)
        assert result["voicemails"] == []
        assert result["empty_reason"] == "no_records"
        assert "error" not in result

    def test_listed_but_unreadable_voicemail_db_is_an_error(self):
        result = _list_voicemails(_FakeBackup(encrypted=True, unreadable=[VOICEMAIL_DB]))
        assert result["voicemails"] == []
        assert "could not be read" in result["error"]
        assert "empty_reason" not in result

    def test_voicemails_present_have_no_empty_reason(self, tmp_path):
        db = _voicemail_db(tmp_path / "vm.db", rows=[(1, "+15551234567", 1700000000, 12)])
        result = _list_voicemails(_FakeBackup({VOICEMAIL_DB: db}))
        assert len(result["voicemails"]) == 1
        assert "empty_reason" not in result
        assert "error" not in result


def _write_unencrypted_backup(backup_dir, files):
    """Minimal on-disk unencrypted backup holding ``files`` (relative path -> local file)."""
    os.makedirs(backup_dir)
    with open(os.path.join(backup_dir, "Manifest.plist"), "wb") as f:
        plistlib.dump({"IsEncrypted": False}, f)
    conn = sqlite3.connect(os.path.join(backup_dir, "Manifest.db"))
    conn.execute(
        "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
        "relativePath TEXT, flags INTEGER, file BLOB)"
    )
    for rel_path, src in files.items():
        file_id = hashlib.sha1(f"HomeDomain-{rel_path}".encode()).hexdigest()
        os.makedirs(os.path.join(backup_dir, file_id[:2]), exist_ok=True)
        with open(src, "rb") as fin, open(os.path.join(backup_dir, file_id[:2], file_id), "wb") as fout:
            fout.write(fin.read())
        conn.execute(
            "INSERT INTO Files VALUES (?, 'HomeDomain', ?, 1, NULL)", (file_id, rel_path)
        )
    conn.commit()
    conn.close()


class TestLocalBackupAccessor:
    """The same cases through the real accessor reading a backup from disk."""

    def test_unencrypted_backup_without_calls_or_voicemail(self, tmp_path):
        backup_dir = str(tmp_path / "backup")
        _write_unencrypted_backup(backup_dir, {})
        backup = open_local_backup(backup_dir)

        calls = _list_calls(backup)
        assert calls["empty_reason"] == "requires_encryption"
        assert "encrypt" in calls["error"].lower()

        voicemails = _list_voicemails(backup)
        assert voicemails == {"voicemails": [], "empty_reason": "not_in_backup"}

    def test_unencrypted_backup_with_empty_databases(self, tmp_path):
        backup_dir = str(tmp_path / "backup")
        _write_unencrypted_backup(backup_dir, {
            CALL_DB: _call_db(tmp_path / "calls.db"),
            VOICEMAIL_DB: _voicemail_db(tmp_path / "vm.db"),
        })
        backup = open_local_backup(backup_dir)

        calls = _list_calls(backup)
        assert calls["empty_reason"] == "no_records"
        assert "error" not in calls

        voicemails = _list_voicemails(backup)
        assert voicemails == {"voicemails": [], "empty_reason": "no_records"}
