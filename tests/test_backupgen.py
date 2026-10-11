"""Tests for backupgen, the synthetic backup generator (dev tooling outside src/)."""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen import SCHEMAS_DIR  # noqa: E402
from backupgen.container import BackupWriter, Device  # noqa: E402
from backupgen.crypto import aes_wrap  # noqa: E402
from backupgen.generate import EXPECTED, generate  # noqa: E402
from backupgen.persona import PersonaError, load_persona  # noqa: E402
from backupgen.sqlitegen import build, load_schema, schema_objects  # noqa: E402
from backupgen.typedstream import attributed_body  # noqa: E402
from ios_backup_core.backup import open_local_backup  # noqa: E402
from ios_backup_core.contacts import ContactResolver  # noqa: E402
from ios_backup_core.extractors.browser_history import BrowserHistoryExtractor  # noqa: E402
from ios_backup_core.extractors.calls import CallExtractor  # noqa: E402
from ios_backup_core.extractors.messages import MessageExtractor  # noqa: E402
from ios_backup_core.extractors.notes import NoteExtractor  # noqa: E402
from ios_backup_core.extractors.photos import PhotoExtractor  # noqa: E402
from ios_backup_core.extractors.voicemail import VoicemailExtractor  # noqa: E402
from ios_backup_core.text import parse_attributed_body  # noqa: E402

SCHEMAS = sorted(
    (folder.name.removeprefix("ios-"), path.name.removesuffix(".json"))
    for folder in SCHEMAS_DIR.iterdir()
    for path in folder.glob("*.json")
    if path.name != "container.json"
)


# ── building blocks ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["Hi", "Grüß dich, Dollie! 🎻", "x" * 127, "y" * 128, "z" * 40_000, "שלום 你好"])
def test_attributed_body_round_trips(text):
    assert parse_attributed_body(attributed_body(text))[0] == text


def test_aes_wrap_matches_rfc3394_vector():
    kek = bytes.fromhex("000102030405060708090A0B0C0D0E0F101112131415161718191A1B1C1D1E1F")
    key = bytes.fromhex("00112233445566778899AABBCCDDEEFF000102030405060708090A0B0C0D0E0F")
    assert aes_wrap(kek, key).hex().upper() == (
        "28C9F404C4B810F4CBCCB35CFB87F8263F5786E2D80ED326CBC7F0E71A99F43BFB988B9B7A02DD21"
    )


@pytest.mark.parametrize("version, database", SCHEMAS)
def test_built_schema_is_identical_to_harvested(tmp_path, version, database):
    path = tmp_path / database
    build(path, version, database, lambda db: None)
    built = sorted((n, s) for _, n, s in schema_objects(path) if not n.startswith("sqlite_"))
    harvested = sorted((o["name"], o["sql"]) for o in load_schema(version, database)["objects"])
    assert built == harvested
    wal = load_schema(version, database)["pragmas"]["journal_mode"] == "wal"
    assert path.read_bytes()[18] == (2 if wal else 1), "journal mode should match the device's"


def test_missing_schema_falls_back_to_the_nearest_older_version():
    assert load_schema("26.5", "NoteStore.sqlite")["ios"] == "17.3"
    assert load_schema("16.2", "sms.db")["ios"] == "16.1.2"
    assert load_schema("26.5", "Photos.sqlite")["ios"] == "26.5"
    with pytest.raises(FileNotFoundError):
        load_schema("12.0", "sms.db")


def test_harvested_schemas_hold_no_rows_or_identifiers():
    text = "".join(p.read_text(encoding="utf-8") for p in SCHEMAS_DIR.rglob("*.json"))
    assert "@" not in text.replace("en-US@collation", "").replace("en@collation", "")
    assert '"rows"' not in text


# ── container ────────────────────────────────────────────────────────────────


def _device():
    return Device(name="Test iPhone", product_type="iPhone12,1", product_name="iPhone 11",
                  ios="15.3.1", build="19D52", phone_number="+44 7700 900001")


@pytest.mark.parametrize("password", [None, "correct horse"])
def test_container_round_trip(tmp_path, password):
    payload = bytes(range(256)) * 50
    writer = BackupWriter(tmp_path, _device(), datetime(2022, 3, 2, tzinfo=timezone.utc), "15.3.1", password=password, seed=1)
    writer.add_file("HomeDomain", "Library/SMS/sms.db", payload)
    backup_dir = writer.finalize()
    accessor = open_local_backup(str(backup_dir), password=password)
    assert Path(accessor.get_file("Library/SMS/sms.db", "HomeDomain")).read_bytes() == payload


def test_wrong_password_is_rejected(tmp_path):
    from iphone_backup_decrypt import EncryptedBackup
    from iphone_backup_decrypt.exceptions import IncorrectPassphraseError

    writer = BackupWriter(tmp_path, _device(), datetime(2022, 3, 2, tzinfo=timezone.utc), "15.3.1", password="right", seed=1)
    writer.add_file("HomeDomain", "Library/test.txt", b"x")
    backup_dir = writer.finalize()
    with pytest.raises(IncorrectPassphraseError):
        EncryptedBackup(backup_directory=str(backup_dir), passphrase="wrong").test_decryption()


def test_real_iteration_counts_still_open(tmp_path):
    writer = BackupWriter(tmp_path, _device(), datetime(2022, 3, 2, tzinfo=timezone.utc), "15.3.1",
                          password="slow", iterations="real", seed=1)
    writer.add_file("HomeDomain", "Library/test.txt", b"slow but real")
    accessor = open_local_backup(str(writer.finalize()), password="slow")
    assert Path(accessor.get_file("Library/test.txt", "HomeDomain")).read_bytes() == b"slow but real"


# ── whole snapshots against their answer keys ────────────────────────────────


def _read(backup_dir, password):
    accessor = open_local_backup(str(backup_dir), password=password)
    resolver = ContactResolver()
    contacts = resolver.load_contacts(accessor)
    extractor = MessageExtractor()
    conversations = extractor.list_conversations(accessor, contacts)["conversations"]
    messages = []
    for conv in conversations:
        for m in extractor.get_messages(accessor, conv["chat_id"], contacts, 0, 10_000)["messages"]:
            messages.append((conv["chat_identifier"], m["text"], m["is_from_me"], m["is_reaction"]))
    return resolver.list_contacts(accessor)["contacts"], conversations, messages


@pytest.mark.parametrize("persona", ["testpersona", "einstein"])
@pytest.mark.parametrize("snapshot", ["ios15", "ios17", "ios26"])
@pytest.mark.parametrize("password", [None, "test-password"])
def test_snapshot_reads_back_as_expected(tmp_path, persona, snapshot, password):
    backup_dir = generate(persona, snapshot, tmp_path, password=password)
    expected = json.loads((tmp_path / EXPECTED).read_text(encoding="utf-8"))
    contacts, conversations, messages = _read(backup_dir, password)
    assert len(contacts) == len(expected["contacts"])
    assert {c["chat_identifier"] for c in conversations} == {c["chat_identifier"] for c in expected["conversations"]}
    for conv in conversations:
        want = next(c for c in expected["conversations"] if c["chat_identifier"] == conv["chat_identifier"])
        assert conv["message_count"] == want["message_count"]
        if want["is_group"]:
            assert conv["display_name"] == want["name"]
    assert sorted(messages) == sorted(
        (m["chat_identifier"], m["text"], m["is_from_me"], m["is_reaction"]) for m in expected["messages"]
    )


def test_snapshots_only_hold_what_existed_by_their_date(tmp_path):
    counts = {}
    for snapshot in ("ios15", "ios17", "ios26"):
        generate("testpersona", snapshot, tmp_path / snapshot)
        counts[snapshot] = json.loads((tmp_path / snapshot / EXPECTED).read_text())
    assert len(counts["ios15"]["messages"]) < len(counts["ios17"]["messages"]) < len(counts["ios26"]["messages"])
    assert all(m["date"] <= counts["ios15"]["backup_date"] for m in counts["ios15"]["messages"])
    assert len(counts["ios15"]["contacts"]) < len(counts["ios17"]["contacts"])  # Casey was added in 2023


def test_answer_key_lists_absent_data_types(tmp_path):
    generate("einstein", "ios15", tmp_path)
    expected = json.loads((tmp_path / EXPECTED).read_text())
    assert expected["absent"] == {t: "none" for t in ("calls", "voicemail", "notes", "photos", "browser_history",
                                                      "calendar", "voice_memos", "health")}


@pytest.mark.parametrize("snapshot", [
    pytest.param("ios15", marks=pytest.mark.xfail(
        strict=True, reason="iOS 15 stores ZADDRESS as a BLOB, which crashes list_calls; fixed by PR #12")),
    "ios17", "ios26",
])
def test_calls_and_voicemails_read_back(tmp_path, snapshot):
    backup_dir = generate("testpersona", snapshot, tmp_path, password="pw")
    expected = json.loads((tmp_path / EXPECTED).read_text(encoding="utf-8"))
    accessor = open_local_backup(str(backup_dir), password="pw")
    contacts = ContactResolver().load_contacts(accessor)
    calls = CallExtractor().list_calls(accessor, contacts, 0, 1000)["calls"]
    # Voicemails also show as missed calls; each one here has its missed call, so none are added.
    assert sorted((c["address"], c["date"], c["duration"], c["direction"]) for c in calls) == sorted(
        (c["address"], c["date"], c["duration"], "outgoing" if c["outgoing"] else "incoming") for c in expected["calls"]
    )
    voicemails = VoicemailExtractor().list_voicemails(accessor, contacts)["voicemails"]
    assert sorted((v["phone_number"], v["date_received"], v["duration"]) for v in voicemails) == sorted(
        (v["sender"], v["date"], v["duration"]) for v in expected["voicemails"]
    )
    for v in expected["voicemails"]:
        audio = Path(VoicemailExtractor().get_audio_path(accessor, v["rowid"])).read_bytes()
        assert audio.startswith(b"#!AMR\n") and len(audio) == 6 + 32 * 50 * v["duration"]


@pytest.mark.parametrize("snapshot", ["ios15", "ios17", "ios26"])
def test_notes_and_safari_read_back(tmp_path, snapshot):
    backup_dir = generate("testpersona", snapshot, tmp_path, password="pw")
    expected = json.loads((tmp_path / EXPECTED).read_text(encoding="utf-8"))
    accessor = open_local_backup(str(backup_dir), password="pw")
    notes = NoteExtractor().list_notes(accessor)["notes"]
    assert sorted((n["title"], n["body"], n["modified"]) for n in notes) == sorted(
        (n["title"], n["text"], n["modified"]) for n in expected["notes"]
    )
    visits = BrowserHistoryExtractor().list_browser_history(accessor)["visits"]
    assert sorted((v["url"], v["title"], v["visit_date"]) for v in visits) == sorted(
        (v["url"], v["title"], v["date"]) for v in expected["browser_history"]
    )
    assert {v["domain"] for v in visits} <= {"en.wikipedia", "example", "timeanddate"}


def test_note_text_keeps_list_items_without_their_markers(tmp_path):
    generate("testpersona", "ios26", tmp_path)
    notes = {n["title"]: n for n in json.loads((tmp_path / EXPECTED).read_text(encoding="utf-8"))["notes"]}
    assert notes["Release checklist"]["text"].split("\n")[1:4] == ["Tag the build", "Write the notes", "Tell support"]
    assert notes["Release checklist"]["folder"] == "Work" and notes["Release checklist"]["pinned"]
    assert notes["Groceries"]["folder"] == "Notes"


@pytest.mark.xfail(strict=True, reason="Photos.sqlite ships without -shm and main's read-only open fails; fixed by PR #12")
@pytest.mark.parametrize("snapshot", ["ios15", "ios26"])
def test_photos_read_back(tmp_path, snapshot):
    backup_dir = generate("testpersona", snapshot, tmp_path)
    expected = json.loads((tmp_path / EXPECTED).read_text(encoding="utf-8"))
    accessor = open_local_backup(str(backup_dir))
    photos = PhotoExtractor().list_photos(accessor, 0, 100)["photos"]
    assert sorted((p["filename"], p["width"], p["height"], p["favorite"]) for p in photos) == sorted(
        (p["filename"], p["width"], p["height"], p["favorite"]) for p in expected["photos"] if not p["trashed"]
    )


def test_photos_look_like_the_device_wrote_them(tmp_path):
    from PIL import Image

    backup_dir = generate("testpersona", "ios26", tmp_path)
    expected = {p["id"]: p for p in json.loads((tmp_path / EXPECTED).read_text(encoding="utf-8"))["photos"]}
    accessor = open_local_backup(str(backup_dir))
    with Image.open(accessor.get_file("Media/DCIM/100APPLE/IMG_0002.JPG", "CameraRollDomain")) as image:
        exif = image.getexif()
        assert (exif[0x010F], exif[0x0110]) == ("Apple", "iPhone 15") and image.size == (600, 800)
        assert exif.get_ifd(0x8825)[1] == "N" and exif.get_ifd(0x8825)[3] == "W"
        assert exif.get_ifd(0x8769)[0x9011] == "+01:00"  # London in April
    db = sqlite3.connect(Path(accessor.get_file("Media/PhotoData/Photos.sqlite", "CameraRollDomain")).as_uri() + "?immutable=1", uri=True)
    rows = dict(db.execute("SELECT ZFILENAME, ZLATITUDE FROM ZASSET"))
    assert rows[expected["desk"]["filename"]] == -180.0  # no location
    assert db.execute("SELECT COUNT(*) FROM Z_RT_Asset_boundedByRect").fetchone()[0] == 4
    albums = dict(db.execute("SELECT ZTITLE, ZCACHEDCOUNT FROM ZGENERICALBUM WHERE ZKIND = 2"))
    assert albums == {"Weekends": 2, "Best": 2}  # the trashed photo leaves its album
    assert db.execute("SELECT ZTRASHEDSTATE FROM ZASSET WHERE ZFILENAME = ?", (expected["blurry"]["filename"],)).fetchone() == (1,)


def test_photo_messages_carry_the_camera_roll_jpeg(tmp_path):
    backup_dir = generate("testpersona", "ios17", tmp_path, password="pw")
    expected = json.loads((tmp_path / EXPECTED).read_text(encoding="utf-8"))
    sent = next(m for m in expected["messages"] if m["attachment"])
    assert sent["text"] == "" and sent["is_from_me"]
    assert any(m["text"] == "Loved an image" for m in expected["messages"])
    accessor = open_local_backup(str(backup_dir), password="pw")
    contacts = ContactResolver().load_contacts(accessor)
    extractor = MessageExtractor()
    chat = next(c for c in extractor.list_conversations(accessor, contacts)["conversations"]
                if c["chat_identifier"] == sent["chat_identifier"])
    message = next(m for m in extractor.get_messages(accessor, chat["chat_id"], contacts, 0, 100)["messages"]
                   if m["has_attachments"])
    attachment = message["attachments"][0]
    assert attachment["mime_type"] == "image/jpeg" and attachment["total_bytes"] == sent["attachment"]["bytes"]
    sent_file = Path(accessor.get_file(attachment["filename"].removeprefix("~/"), "MediaDomain")).read_bytes()
    camera_roll = Path(accessor.get_file("Media/DCIM/100APPLE/IMG_0002.JPG", "CameraRollDomain")).read_bytes()
    assert sent_file == camera_roll


def test_unencrypted_backups_leave_out_call_history(tmp_path):
    backup_dir = generate("testpersona", "ios26", tmp_path)
    expected = json.loads((tmp_path / EXPECTED).read_text())
    assert expected["calls"] == [] and expected["absent"]["calls"] == "encrypted_only"
    assert expected["browser_history"] == [] and expected["absent"]["browser_history"] == "encrypted_only"
    assert len(expected["voicemails"]) == 3
    manifest = sqlite3.connect(backup_dir / "Manifest.db")
    paths = {r[0] for r in manifest.execute("SELECT relativePath FROM Files")}
    assert "Library/Voicemail/voicemail.db" in paths and "NoteStore.sqlite" in paths
    assert not any("CallHistory" in p or "Safari" in p for p in paths)


def test_encrypting_changes_only_what_apple_leaves_out(tmp_path):
    generate("testpersona", "ios26", tmp_path / "plain", seed=7)
    generate("testpersona", "ios26", tmp_path / "enc", password="pw", seed=7)
    plain, enc = (json.loads((tmp_path / d / EXPECTED).read_text()) for d in ("plain", "enc"))
    for key in ("contacts", "conversations", "voicemails", "notes", "photos"):
        assert plain[key] == enc[key], key


def test_messages_in_one_minute_keep_their_order(tmp_path):
    import shutil

    shutil.copytree(SCHEMAS_DIR.parent / "personas" / "testpersona", tmp_path / "packs" / "testpersona")
    (tmp_path / "packs" / "testpersona" / "chats" / "burst.txt").write_text(
        "chat: burst\nwith: riley, jordan\nname: Burst\n\n== 2024-01-01\n"
        "12:00 riley: one\n12:00 me: two\n12:00 jordan +like\n12:00 me: three\n", encoding="utf-8")
    persona = load_persona("testpersona", root=tmp_path / "packs")
    for seed in range(20):  # independent random seconds used to reorder these for some seeds
        generate("testpersona", "ios26", tmp_path / str(seed), seed=seed, persona=persona)
        messages = json.loads((tmp_path / str(seed) / EXPECTED).read_text(encoding="utf-8"))["messages"]
        burst = [m["text"] for m in messages if m["date"].startswith("2024-01-01T12:0")]
        assert burst == ["one", "two", "Liked “two”", "three"], seed


def test_same_phone_keeps_its_udid(tmp_path):
    udids = {}
    for snapshot, password in [("ios17", None), ("ios26", None), ("ios26", "pw")]:
        out = tmp_path / f"{snapshot}-{password}"
        udids[(snapshot, password)] = generate("testpersona", snapshot, out, password=password).name
    assert len(set(udids.values())) == 1, udids


def test_generation_is_deterministic(tmp_path):
    a = generate("testpersona", "ios17", tmp_path / "a", password="pw")
    b = generate("testpersona", "ios17", tmp_path / "b", password="pw")
    files_a = {p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()}
    files_b = {p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()}
    assert files_a == files_b


def test_daylight_saving_follows_the_persona_time_zone(tmp_path):
    generate("testpersona", "ios15", tmp_path)
    expected = json.loads((tmp_path / EXPECTED).read_text())
    before = next(m for m in expected["messages"] if m["text"] == "Clocks go forward tonight 🕐")
    after = next(m for m in expected["messages"] if m["text"].startswith("Lost an hour"))
    assert before["date"].endswith("+00:00")  # 23:50 GMT
    assert datetime.fromisoformat(after["date"]).hour == 8  # 09:05 BST is 08:05 UTC


# ── persona validation ───────────────────────────────────────────────────────


def _pack(tmp_path, chat_text):
    folder = tmp_path / "packs" / "p"
    (folder / "chats").mkdir(parents=True)
    source = json.loads((SCHEMAS_DIR.parent / "personas" / "testpersona" / "persona.json").read_text())
    source["id"] = "p"
    (folder / "persona.json").write_text(json.dumps(source))
    (folder / "chats" / "c.txt").write_text(chat_text)
    return folder.parent


def test_photos_cannot_be_sent_before_they_are_taken(tmp_path):
    import shutil

    shutil.copytree(SCHEMAS_DIR.parent / "personas" / "testpersona", tmp_path / "testpersona")
    chat = tmp_path / "testpersona" / "chats" / "riley.txt"
    chat.write_text(chat.read_text(encoding="utf-8").replace("10:25 me [photo:london-2]", "09:00 me [photo:london-2]"),
                    encoding="utf-8")
    with pytest.raises(PersonaError, match="sent before it was taken"):
        load_persona("testpersona", root=tmp_path)


@pytest.mark.parametrize("chat_text, message", [
    ("chat: c\nwith: nobody\n\n== 2021-01-01\n10:00 me: hi\n", "unknown participant"),
    ("chat: c\nwith: riley\n\n== 2021-01-01\n10:00 me: hi\n09:00 riley: earlier\n", "time order"),
    ("chat: c\nwith: riley\n\n== 2021-01-01\n10:00 riley: hi\n10:01 me +hug\n", "unknown reaction"),
    ("chat: c\nwith: riley\n\n== 2030-01-01\n10:00 me: hi\n", "outside the persona's period"),
    ("chat: c\nwith: riley\n\n== 2021-01-01\nnot a message\n", "can't parse"),
])
def test_bad_chat_files_are_rejected(tmp_path, chat_text, message):
    with pytest.raises(PersonaError, match=message):
        load_persona("p", root=_pack(tmp_path, chat_text))


def test_sms_databases_have_no_wal_files(tmp_path):
    backup_dir = generate("testpersona", "ios15", tmp_path)
    manifest = sqlite3.connect(backup_dir / "Manifest.db")
    paths = [r[0] for r in manifest.execute("SELECT relativePath FROM Files")]
    assert "Library/SMS/sms.db" in paths and not any(p.endswith(("-wal", "-shm")) for p in paths)


@pytest.mark.parametrize("password", [None, "pw"])
def test_manifest_db_matches_real_backups(tmp_path, password):
    """Real Manifest.db files are WAL databases with user_version 2 (no -wal/-shm in the backup)."""
    from iphone_backup_decrypt import EncryptedBackup

    backup_dir = generate("testpersona", "ios17", tmp_path, password=password)
    path = backup_dir / "Manifest.db"
    if password:
        path = tmp_path / "decrypted.db"
        EncryptedBackup(backup_directory=str(backup_dir), passphrase=password).save_manifest_file(str(path))
    header = path.read_bytes()[:100]
    assert header[18] == 2, "WAL mode"
    assert int.from_bytes(header[60:64], "big") == 2, "user_version"
    assert not any((backup_dir / f"Manifest.db{s}").exists() for s in ("-wal", "-shm"))
