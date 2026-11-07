"""Backups made by iOS 9 and earlier: Manifest.mbdb, files flat in the folder, older databases.

openextract in-app report OE-13 ("Can't open old backup with manifest.mbdb
file"). Encrypted ones are decrypted by ios_backup_core.legacy_crypto, since
iphone-backup-decrypt reads only iOS 10.2+ encryption. The backups come from
backupgen.legacy, which writes the older table
layouts: its iOS 6 sms.db has no associated_message_type, chat.display_name or
attachment.transfer_name; call history is call_history.db (Unix dates, a flags
field) before iOS 8; Photos.sqlite has no ZTRASHEDSTATE before iOS 8; notes are
HTML in notes.sqlite before iOS 9.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen.legacy import generate_legacy  # noqa: E402
from ios_backup_core.backup import (  # noqa: E402
    DAMAGED_MBDB_MESSAGE,
    BackupReader,
    LocalBackupAccessor,
    _read_backup_info,
    manifest_format,
    open_local_backup,
)
from ios_backup_core.contacts import ContactResolver  # noqa: E402
from ios_backup_core.extractors.messages import MessageExtractor  # noqa: E402
from ios_backup_core.extractors.photos import PhotoExtractor  # noqa: E402

PASSWORD = "test"
# (iOS version, password): every release unencrypted, the oldest and newest encrypted.
BACKUPS = [("6.1.6", None), ("7.1.2", None), ("8.4.1", None), ("9.3.5", None),
           ("6.1.6", PASSWORD), ("9.3.5", PASSWORD)]


@pytest.fixture(scope="module", params=BACKUPS, ids=lambda b: f"{b[0]}-{'encrypted' if b[1] else 'plain'}")
def legacy(request, tmp_path_factory):
    ios, password = request.param
    out = tmp_path_factory.mktemp(f"legacy-{ios}")
    backup_dir = generate_legacy(out, ios=ios, password=password)
    expected = json.loads((out / "expected.json").read_text(encoding="utf-8"))
    reader = BackupReader.from_path(str(backup_dir), password=password)
    yield backup_dir, expected, reader
    reader._accessor.cleanup()


def test_backup_info_comes_from_the_same_plists(legacy):
    backup_dir, expected, reader = legacy
    info = reader._accessor.info
    assert info["manifest_format"] == "mbdb"
    assert info["product_version"] == expected["ios"]
    assert info["device_name"] == "Sam's iPhone"
    assert info["encrypted"] is reader._accessor.encrypted
    assert info["udid"].lower() == backup_dir.name
    assert info["last_backup"].startswith("2016-09-10T18:30:00")
    assert manifest_format(str(backup_dir)) == "mbdb"


def test_files_are_found_flat_in_the_folder(legacy):
    backup_dir, _, reader = legacy
    stored = backup_dir / "3d0d7e5fb2ce288813306e4d4636395e047a3d28"
    path = reader._accessor.get_file("Library/SMS/sms.db")
    if reader._accessor.encrypted:
        assert Path(path).read_bytes()[:16] == b"SQLite format 3\x00"
        assert stored.read_bytes()[:16] != b"SQLite format 3\x00"
    else:
        assert path == str(stored)
    assert reader._accessor.get_file("Library/Nope.db") is None
    assert not any(p.is_dir() for p in backup_dir.iterdir())


def test_list_files_reads_the_mbdb(legacy):
    _, expected, reader = legacy
    dcim = reader._accessor.list_files(domain="CameraRollDomain", path_like="Media/DCIM/%")
    files = {f["path"] for f in dcim}
    assert {p["relative_path"] for p in expected["photos"]} <= files
    assert all(len(f["hash"]) == 40 for f in dcim)


def test_messages(legacy):
    _, expected, reader = legacy
    conversations = reader.messages()["conversations"]
    assert len(conversations) == len(expected["conversations"])
    by_identifier = {c["chat_identifier"]: c for c in conversations}
    for want in expected["conversations"]:
        got = by_identifier[want["chat_identifier"]]
        assert got["service"] == want["service"]
        assert got["message_count"] == len(want["messages"])
        if want["display_name"]:
            assert got["display_name"] == want["display_name"]
        else:
            # No chat.display_name (iOS 6): named after its people.
            for name in want["participants"]:
                assert name in got["display_name"]
        messages = reader.messages(chat_id=got["chat_id"])["messages"]
        assert [m["date"] for m in messages] == [m["date"] for m in want["messages"]]
        assert [m["text"] for m in messages] == [m["text"] for m in want["messages"]]
        assert [m["is_from_me"] for m in messages] == [m["is_from_me"] for m in want["messages"]]
        assert not any(m["is_reaction"] for m in messages)
        for got_m, want_m in zip(messages, want["messages"]):
            if want_m["attachment"]:
                (attachment,) = got_m["attachments"]
                assert want_m["attachment"] in (attachment["transfer_name"] or attachment["filename"])
                rel = attachment["filename"].removeprefix("~/")
                assert reader._accessor.get_file(rel, domain="MediaDomain")


def test_search_and_date_filters_use_seconds(legacy):
    _, _, reader = legacy
    extractor = MessageExtractor()
    contacts = reader.contacts()
    hits = extractor.search_messages(reader._accessor, "Saturday", contacts)["results"]
    assert [h["text"] for h in hits] == ["Are we still on for Saturday?"]
    hits = extractor.search_messages(reader._accessor, "a", contacts, date_from="2013-01-01")["results"]
    assert hits and all(h["date"] >= "2013" for h in hits)


def test_contacts(legacy):
    _, expected, reader = legacy
    contacts = ContactResolver().list_contacts(reader._accessor)["contacts"]
    assert sorted(c["display_name"] for c in contacts) == expected["contacts"]


def test_calls(legacy):
    _, expected, reader = legacy
    result = reader.calls()
    got = sorted(result["calls"], key=lambda c: c["date"])
    want = sorted(expected["calls"], key=lambda c: c["date"])
    assert [c["date"] for c in got] == [c["date"] for c in want]
    for g, w in zip(got, want):
        assert g["contact_name"] == w["contact_name"]
        assert (g["direction"], g["status"], g["app"]) == (w["direction"], w["status"], w["app"])
        assert g["duration"] == w["duration"]


def test_photos(legacy):
    _, expected, reader = legacy
    extractor = PhotoExtractor()
    result = extractor.list_photos(reader._accessor)
    assert "error" not in result
    got = {p["filename"]: p for p in result["photos"]}
    for want in expected["photos"]:
        photo = got[want["filename"]]
        assert photo["date_created"] == want["date_created"]
        assert photo["latitude"] == want["latitude"]
        assert photo["relative_path"] == want["relative_path"]
        path = extractor.get_file_path(reader._accessor, photo)
        assert Path(path).read_bytes()[:3] == b"\xff\xd8\xff"
    albums = {a["title"]: a for a in extractor.list_albums(reader._accessor)["albums"]}
    assert albums["Holiday"]["asset_count"] == sum(1 for p in expected["photos"] if p["album"] == "Holiday")
    holiday = extractor.list_photos(reader._accessor, album_id=albums["Holiday"]["id"])
    assert {p["filename"] for p in holiday["photos"]} == {p["filename"] for p in expected["photos"]
                                                          if p["album"] == "Holiday"}


def test_notes(legacy):
    _, expected, reader = legacy
    notes = reader.notes()["notes"]
    assert sorted(n["title"] for n in notes) == sorted(n["title"] for n in expected["notes"])
    for note in notes:
        assert note["body"] == expected["note_bodies"][note["title"]]
        want = next(n for n in expected["notes"] if n["title"] == note["title"])
        assert (note["created"], note["modified"]) == (want["created"], want["modified"])


def test_what_old_backups_dont_have_comes_back_empty(legacy):
    _, _, reader = legacy
    assert reader.voicemail()["voicemails"] == []
    assert reader.browser_history()["visits"] == []


# ── encrypted, damaged, and older formats ────────────────────────────────────


def test_an_encrypted_legacy_backup_needs_the_right_password(tmp_path):
    backup_dir = generate_legacy(tmp_path, ios="9.3.5", password=PASSWORD)
    info = _read_backup_info(str(backup_dir))
    assert (info["encrypted"], info["manifest_format"]) == (True, "mbdb")
    with pytest.raises(ValueError, match="encrypted"):
        open_local_backup(str(backup_dir))
    with pytest.raises(ValueError, match="incorrect passphrase"):
        open_local_backup(str(backup_dir), password="wrong")
    accessor = open_local_backup(str(backup_dir), password=PASSWORD)
    assert accessor.encrypted
    accessor.cleanup()


def test_a_damaged_mbdb_in_an_encrypted_backup_says_so(tmp_path):
    backup_dir = generate_legacy(tmp_path, ios="9.3.5", password=PASSWORD)
    mbdb = backup_dir / "Manifest.mbdb"
    mbdb.write_bytes(mbdb.read_bytes()[:-10])
    with pytest.raises(ValueError) as caught:
        open_local_backup(str(backup_dir), password=PASSWORD)
    assert str(caught.value) == DAMAGED_MBDB_MESSAGE


def test_a_damaged_mbdb_fails_at_open_with_a_clear_message(tmp_path):
    backup_dir = generate_legacy(tmp_path, ios="9.3.5")
    mbdb = backup_dir / "Manifest.mbdb"
    mbdb.write_bytes(mbdb.read_bytes()[:-10])
    with pytest.raises(ValueError) as caught:
        open_local_backup(str(backup_dir))
    assert str(caught.value) == DAMAGED_MBDB_MESSAGE


def test_a_damaged_mbdb_on_a_direct_accessor_reads_as_empty(tmp_path):
    backup_dir = generate_legacy(tmp_path, ios="9.3.5")
    (backup_dir / "Manifest.mbdb").write_bytes(b"mbdb\x05\x00\x00")
    info = _read_backup_info(str(backup_dir))
    accessor = LocalBackupAccessor(str(backup_dir), info["udid"], info, encrypted=False)
    assert accessor.list_files() == []
    assert accessor.get_file("Library/SMS/sms.db") is None
    with pytest.raises(ValueError):
        accessor.check_manifest()
    accessor.cleanup()


def test_an_ios5_sms_db_is_reported_not_crashed(tmp_path):
    backup_dir = generate_legacy(tmp_path, ios="6.1.6")
    reader = BackupReader.from_path(str(backup_dir))
    sms = Path(reader._accessor.get_file("Library/SMS/sms.db"))
    # iOS 5's sms.db: messages hang off msg_group; no chat or chat_message_join.
    sms.unlink()
    conn = sqlite3.connect(sms)
    conn.executescript("""
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT, date INTEGER,
            text TEXT, flags INTEGER, replace INTEGER, svc_center TEXT, group_id INTEGER);
        CREATE TABLE msg_group (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER, newest_message INTEGER);
    """)
    conn.close()
    result = MessageExtractor().list_conversations(reader._accessor, {})
    assert result["conversations"] == []
    assert "iOS 5" in result["error"]
    assert MessageExtractor().search_messages(reader._accessor, "x", {})["results"] == []
    reader._accessor.cleanup()


def test_legacy_backup_is_not_written_to(tmp_path):
    backup_dir = generate_legacy(tmp_path, ios="7.1.2")
    before = {p.name: p.stat().st_mtime_ns for p in backup_dir.iterdir()}
    reader = BackupReader.from_path(str(backup_dir))
    reader.messages()
    reader.calls()
    reader.notes()
    PhotoExtractor().list_photos(reader._accessor)
    reader._accessor.cleanup()
    assert {p.name: p.stat().st_mtime_ns for p in backup_dir.iterdir()} == before


def test_modern_backup_with_a_stray_mbdb_still_reads_manifest_db(tmp_path):
    backup_dir = generate_legacy(tmp_path, ios="9.3.5")
    # A folder with both (an old backup a newer iTunes added to): Manifest.db wins.
    conn = sqlite3.connect(backup_dir / "Manifest.db")
    conn.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")
    conn.commit()
    conn.close()
    assert manifest_format(str(backup_dir)) == "db"
    accessor = open_local_backup(str(backup_dir))
    assert accessor.info["manifest_format"] == "db"
    assert accessor.list_files() == []
    accessor.cleanup()
