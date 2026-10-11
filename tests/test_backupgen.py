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
from ios_backup_core.extractors.messages import MessageExtractor  # noqa: E402
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
    assert path.read_bytes()[18] == 2, "should be a WAL-mode file, like the device's"


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
    assert expected["absent"] == ["calls", "voicemail", "notes", "photos", "browser_history",
                                  "calendar", "voice_memos", "health"]


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
