"""backupgen's WhatsApp writer: generated chats read back through WhatsAppExtractor as the answer key says.

Maren's pack is the showcase (WhatsApp 2.24's layout, with LID.sqlite). A copy of the test
persona with a small WhatsApp folder covers the older layouts: 15.3.1 (LID.sqlite and
ContactsV2.sqlite, calls kept outside the chats) and 13.4.1 (ContactsV2.sqlite only).
Photo downloads are replaced by small generated images, so nothing here needs the network.
"""

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen import PERSONAS_DIR, media  # noqa: E402
from backupgen.generate import EXPECTED, generate  # noqa: E402
from backupgen.persona import PersonaError, load_persona  # noqa: E402
from backupgen.writers import whatsapp as writer  # noqa: E402
from ios_backup_core.backup import open_local_backup  # noqa: E402
from ios_backup_core.contacts import ContactResolver, resolve_contact  # noqa: E402
from ios_backup_core.extractors.whatsapp import SHARED_DOMAIN, WhatsAppExtractor  # noqa: E402


def _fake_fetch(url, sha1):
    """A small JPEG coloured by the photo's hash, in place of a download."""
    from PIL import Image

    out = io.BytesIO()
    color = "#" + hashlib.sha1(url.encode()).hexdigest()[:6]
    Image.new("RGB", (64, 48), color).save(out, "JPEG")
    return out.getvalue()


def _generate(persona, snapshot, out, password=None, pack=None):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(media, "fetch", _fake_fetch)
        backup_dir = generate(persona, snapshot, out, password=password, persona=pack)
    expected = json.loads((out / EXPECTED).read_text(encoding="utf-8"))
    accessor = open_local_backup(str(backup_dir), password=password)
    contacts = ContactResolver().load_contacts(accessor)
    return SimpleNamespace(backup_dir=backup_dir, expected=expected, wa=expected["whatsapp"],
                           accessor=accessor, contacts=contacts)


@pytest.fixture(scope="module", params=[None, "pw"], ids=["plain", "encrypted"])
def maren(request, tmp_path_factory):
    return _generate("maren", "ios26", tmp_path_factory.mktemp("maren"), password=request.param)


# ── comparing a backup with its answer key ───────────────────────────────────

_CALL_OUTCOMES = {"answered": "answered", "missed": "missed", "unanswered": "no_answer"}


def _read(gen):
    """{chat id in the answer key: (conversation, messages)} as the extractor reads them."""
    extractor = WhatsAppExtractor()
    listed = extractor.list_conversations(gen.accessor, gen.contacts)
    assert "error" not in listed and "empty_reason" not in listed
    by_jid = {c["chat_jid"]: c for c in listed["conversations"]}
    assert set(by_jid) == {c["jid"] for c in gen.wa["chats"]}, "status updates are never chats"
    out = {}
    for chat in gen.wa["chats"]:
        conv = by_jid[chat["jid"]]
        page = extractor.get_messages(gen.accessor, conv["chat_id"], gen.contacts, limit=10_000)
        assert page["total"] == chat["message_count"]
        out[chat["chat"]] = (conv, page["messages"])
    return out


def _expected_messages(gen, chat_id):
    return [m for m in gen.wa["messages"] if m["chat"] == chat_id]


def check_against_answer_key(gen):
    extractor = WhatsAppExtractor()
    read = _read(gen)
    probe = extractor.has_whatsapp(gen.accessor)
    assert probe == {"has_whatsapp": True, "chats": len(gen.wa["chats"]),
                     "messages": sum(c["message_count"] for c in gen.wa["chats"])}
    for chat in gen.wa["chats"]:
        conv, messages = read[chat["chat"]]
        assert (conv["display_name"], conv["chat_type"], conv["handle"], conv["message_count"]) == (
            chat["name"], chat["type"], chat["handle"], chat["message_count"])
        assert datetime.fromisoformat(conv["last_message_date"]) == datetime.fromisoformat(chat["last_message_date"])
        want = _expected_messages(gen, chat["chat"])
        assert [m["sort"] for m in messages] == [w["sort"] for w in want], "arrival order (ZSORT)"
        by_sort = {m["sort"]: m for m in messages}
        sort_of = {m["message_id"]: m["sort"] for m in messages}
        for w in want:
            m = by_sort[w["sort"]]
            where = f"{chat['chat']} #{w['sort']}"
            assert m["message_type"] == w["kind"], where
            assert datetime.fromisoformat(m["date"]) == datetime.fromisoformat(w["date"]), where
            assert m["is_from_me"] == w["is_from_me"], where
            assert ("" if m["is_from_me"] else m["sender_handle"]) == w["sender_handle"], where
            if not m["is_from_me"] and w["sender_name"]:
                assert m["sender"] == w["sender_name"], where
            assert m["text"] == w["text"], where
            assert m["starred"] == w["starred"], where
            if w["quoted"]:
                q = m["quoted"]
                assert q is not None, where
                assert (sort_of[q["message_id"]], q["is_from_me"], q["text"], q["message_type"]) == (
                    w["quoted"]["sort"], w["quoted"]["is_from_me"], w["quoted"]["text"], w["quoted"]["kind"]), where
            else:
                assert m["quoted"] is None, where
            assert [(r["emoji"], r["is_from_me"], "" if r["is_from_me"] else r["sender_handle"],
                     datetime.fromisoformat(r["date"])) for r in m["reactions"]] == [
                (r["emoji"], r["is_from_me"], r["sender_handle"], datetime.fromisoformat(r["date"]))
                for r in w["reactions"]], where
            if w["call"]:
                assert m["call"] == {"is_video": w["call"]["video"], "duration": w["call"]["duration"],
                                     "outcome": _CALL_OUTCOMES[w["call"]["outcome"]],
                                     "direction": w["call"]["direction"]}, where
            if w["location"]:
                loc = m["location"]
                assert (loc["latitude"], loc["longitude"], loc["live"], loc["place_name"], loc["thumbnail_path"]) == (
                    w["location"]["lat"], w["location"]["lon"], w["location"]["live"], w["location"]["place"],
                    w["location"]["thumbnail"]), where
                preview = extractor.get_attachment(gen.accessor, loc["attachment_id"], thumbnail=True)
                assert Path(preview["path"]).read_bytes()[:2] == b"\xff\xd8", where
            if w["notice"]:
                notice = w["notice"]
                if notice["event"] == "encrypted":
                    assert m["event_text"] == "Messages and calls are end-to-end encrypted.", where
                elif notice["event"] == "created":
                    assert m["event_text"] == f"{notice['name']} created the group", where
                else:
                    assert m["event_text"] == ("You were added" if notice["member"] == "me"
                                               else f"{notice['name']} was added"), where
            if w["attachment"]:
                _check_attachment(gen, m, w["attachment"], where)
            else:
                assert m["attachments"] == [], where
    return read


def _check_attachment(gen, message, want, where):
    att = message["attachments"][0]
    assert (att["transfer_name"], att["mime_type"], att["total_bytes"], att["available"], att["thumbnail_path"]) == (
        want["name"], want["mime_type"], want["bytes"], want["available"], want["thumbnail"]), where
    assert att["filename"] == want["path"], where
    assert (att["width"], att["height"]) == (want["width"], want["height"]), where
    assert att["duration"] == want["duration"], where
    extractor = WhatsAppExtractor()
    found = extractor.get_attachment(gen.accessor, att["attachment_id"])
    if not want["available"]:
        assert found == {"error": "Attachment file not found in backup"}, where
    else:
        data = Path(found["path"]).read_bytes()
        assert len(data) == want["bytes"], where
        magic = {"image/jpeg": b"\xff\xd8", "application/pdf": b"%PDF-", "audio/ogg; codecs=opus": b"OggS"}
        assert data.startswith(magic[want["mime_type"]]), where
    if want["thumbnail"]:
        thumb = extractor.get_attachment(gen.accessor, att["attachment_id"], thumbnail=True)
        assert Path(thumb["path"]).read_bytes()[:2] == b"\xff\xd8", where


# ── Maren ────────────────────────────────────────────────────────────────────

def test_maren_reads_back_as_her_answer_key_says(maren):
    check_against_answer_key(maren)


def test_maren_has_every_kind_of_whatsapp_message(maren):
    wa = maren.wa
    assert "whatsapp" not in maren.expected["absent"]
    assert wa["owner"] == "+15035550101" and wa["schema"] == "17.3"
    types = {c["type"] for c in wa["chats"]}
    assert types == {"individual", "group", "broadcast"} and 6 <= len(wa["chats"]) <= 10
    kinds = {m["kind"] for m in wa["messages"]}
    assert {"text", "image", "audio", "document", "shared_location", "live_location", "call", "deleted",
            "system"} <= kinds
    groups = {c["chat"] for c in wa["chats"] if c["type"] == "group"}
    in_groups = [m for m in wa["messages"] if m["chat"] in groups]
    assert any(m["quoted"] for m in in_groups) and any(m["reactions"] for m in in_groups)
    assert {m["notice"]["event"] for m in in_groups if m["notice"]} == {"encrypted", "created", "added"}
    assert {m["call"]["outcome"] for m in wa["messages"] if m["call"]} >= {"answered", "missed"}
    assert any(m["starred"] for m in wa["messages"])
    assert any(m["attachment"] and not m["attachment"]["available"] for m in wa["messages"])
    voice = [m for m in wa["messages"] if m["kind"] == "audio"]
    assert voice and all(m["attachment"]["available"] for m in voice)
    assert wa["status_updates"] == 2 and wa["calls_left_out"] == 0
    dates = sorted(m["date"] for m in wa["messages"])
    assert dates[0] < "2025-01-01" and dates[-1] > "2026-01-01", "spread over her two years"


def test_whatsapp_people_join_their_texts_by_number(maren):
    for chat in maren.wa["chats"]:
        if chat["type"] != "individual":
            continue
        name = resolve_contact(chat["handle"], maren.contacts)
        assert bool(name) == chat["contact"]
        assert chat["name"] == (name or chat["name"])
    text_handles = {c["chat_identifier"] for c in maren.expected["conversations"]}
    assert "+15415550134" in text_handles, "Lucía writes on both"
    only_whatsapp = [c for c in maren.wa["chats"] if c["type"] == "individual" and not c["contact"]]
    assert [c["name"] for c in only_whatsapp] == ["Dani Reyes"]


def test_status_updates_are_not_chats_and_search_skips_them(maren):
    extractor = WhatsAppExtractor()
    assert extractor.search_messages(maren.accessor, "Larches", maren.contacts)["results"] == []
    found = extractor.search_messages(maren.accessor, "lockbox", maren.contacts)["results"]
    assert found and {r["chat_name"] for r in found} == {"Dani Reyes"}
    found = extractor.search_messages(maren.accessor, "house guide", maren.contacts)["results"]
    assert [r["text"] for r in found] == ["Casita del Sol house guide.pdf"]


def test_whatsapp_files_sit_in_the_shared_domain(maren):
    files = maren.accessor.list_files(domain=SHARED_DOMAIN)
    paths = {f["path"] for f in files}
    assert {"ChatStorage.sqlite", "LID.sqlite", writer.PLIST} <= paths
    assert "ContactsV2.sqlite" not in paths, "WhatsApp 2.24 no longer keeps it"
    media_files = [p for p in paths if p.startswith("Message/Media/") and p.count("/") == 5]  # not the folders
    assert media_files and all(re.match(r"^Message/Media/[^/]+@[a-z.]+/[0-9a-f]/[0-9a-f]/[0-9a-f-]{36}\.\w+$", p)
                               for p in media_files)
    total = sum(Path(maren.accessor.get_file(p, SHARED_DOMAIN)).stat().st_size for p in media_files)
    assert total < 5_000_000


# ── older layouts, from a copy of the test persona ───────────────────────────

_PEOPLE = "jordan - Jordan T\nalex +44 7700 900099 Alex Visitor\n"
_JORDAN = """chat: jordan
with: jordan

== 2021-06-01
09:00 notice: encrypted
09:01 jordan: Morning! Did the parcel arrive?
09:05 me > jordan: Yes, thank you!
09:06 jordan +👍
09:10 me [photo:london-2] From the walk yesterday
09:12 jordan [voice:9s]
09:15 jordan [call:voice 3m05s]

== 2023-03-01
18:00 me [call:video unanswered]
18:02* jordan: Sorry, in a meeting. Tomorrow?
18:03 jordan [location:51.5007,-0.1246 Westminster Bridge]
18:04 jordan [document:Agenda.pdf]
18:05 jordan [deleted]
"""
_GROUP = """chat: club
type: group
with: riley, jordan, alex
name: Book club
created_by: riley
addressing: lid

== 2021-09-01
19:00 notice: encrypted
19:00 notice: created
19:00 notice: added me
19:00 notice: added jordan
19:01 riley: Next book?
19:02 alex: Something short please
19:03 me > alex: Agreed
19:03 riley +😂

== 2024-01-10
20:00 jordan [photo:missing] My copy, with coffee on it
20:01 me > jordan: Classic
"""


@pytest.fixture
def pack(tmp_path):
    folder = tmp_path / "packs" / "testpersona"
    shutil.copytree(PERSONAS_DIR / "testpersona", folder)
    raw = json.loads((folder / "persona.json").read_text(encoding="utf-8"))
    raw["snapshots"].append({"id": "ios13", "backup_date": "2024-05-01T20:00:00", "ios": "13.4.1", "build": "17E262",
                             "product_type": "iPhone11,8", "product_name": "iPhone XR"})
    (folder / "persona.json").write_text(json.dumps(raw), encoding="utf-8")
    (folder / "whatsapp" / "documents").mkdir(parents=True)
    (folder / "whatsapp" / "people.txt").write_text(_PEOPLE, encoding="utf-8")
    (folder / "whatsapp" / "jordan.txt").write_text(_JORDAN, encoding="utf-8")
    (folder / "whatsapp" / "club.txt").write_text(_GROUP, encoding="utf-8")
    (folder / "whatsapp" / "documents" / "Agenda.pdf.txt").write_text("Agenda\n1. Coffee\n2. Plans", encoding="utf-8")
    return folder.parent


@pytest.mark.parametrize("snapshot, schema, side_files, calls", [
    ("ios13", "13.4.1", {"ContactsV2.sqlite"}, False),
    ("ios15", "15.3.1", {"ContactsV2.sqlite", "LID.sqlite"}, False),
    ("ios17", "17.3", {"LID.sqlite"}, True),
    ("ios26", "17.3", {"LID.sqlite"}, True),
])
def test_each_layout_reads_back(tmp_path, pack, snapshot, schema, side_files, calls):
    persona = load_persona("testpersona", root=pack)
    gen = _generate("testpersona", snapshot, tmp_path / "out", password="pw" if snapshot == "ios17" else None,
                    pack=persona)
    assert gen.wa["schema"] == schema
    paths = {f["path"] for f in gen.accessor.list_files(domain=SHARED_DOMAIN)}
    assert {p for p in paths if p in ("ContactsV2.sqlite", "LID.sqlite")} == side_files
    has_calls = any(m["call"] for m in gen.wa["messages"])
    assert has_calls == calls and (gen.wa["calls_left_out"] > 0) == (not calls)
    read = check_against_answer_key(gen)
    backup_date = gen.expected["backup_date"]
    assert all(m["date"] <= backup_date for m in gen.wa["messages"])
    names = {m["sender"] for _, messages in read.values() for m in messages if not m["is_from_me"]}
    assert {"Jordan Test", "Riley Sample"} <= names
    if snapshot != "ios15":  # the 2022 backup is from before Alex wrote
        assert "Alex Visitor" in names


def test_a_backup_from_before_the_first_chat_has_no_whatsapp(tmp_path, pack):
    persona = load_persona("testpersona", root=pack)
    for chat in persona.whatsapp:
        for m in chat.messages:
            m.real_time = m.real_time.replace(year=2025)
    gen = _generate("testpersona", "ios15", tmp_path / "out", pack=persona)
    assert gen.wa is None and gen.expected["absent"]["whatsapp"] == "none"


def test_adding_whatsapp_leaves_the_rest_of_the_backup_alone(tmp_path, pack):
    with_wa = load_persona("testpersona", root=pack)
    without = load_persona("testpersona")
    a = _generate("testpersona", "ios26", tmp_path / "a", pack=with_wa)
    b = _generate("testpersona", "ios26", tmp_path / "b", pack=without)
    for key in ("contacts", "conversations", "messages", "voicemails", "notes", "photos"):
        assert a.expected[key] == b.expected[key], key
    assert b.expected["absent"]["whatsapp"] == "none" and "whatsapp" not in a.expected["absent"]


def test_whatsapp_generation_is_deterministic(tmp_path, pack):
    persona = load_persona("testpersona", root=pack)
    a = _generate("testpersona", "ios17", tmp_path / "a", pack=persona)
    b = _generate("testpersona", "ios17", tmp_path / "b", pack=persona)
    assert a.expected["whatsapp"] == b.expected["whatsapp"]
    db = Path(a.accessor.get_file("ChatStorage.sqlite", SHARED_DOMAIN)).read_bytes()
    assert db == Path(b.accessor.get_file("ChatStorage.sqlite", SHARED_DOMAIN)).read_bytes()


def test_core_data_bookkeeping(tmp_path, pack):
    gen = _generate("testpersona", "ios26", tmp_path / "out", pack=load_persona("testpersona", root=pack))
    path = Path(gen.accessor.get_file("ChatStorage.sqlite", SHARED_DOMAIN))
    db = sqlite3.connect(path.as_uri() + "?immutable=1", uri=True)
    maxima = dict(db.execute("SELECT Z_NAME, Z_MAX FROM Z_PRIMARYKEY"))
    assert maxima["WAMessage"] == db.execute("SELECT MAX(Z_PK) FROM ZWAMESSAGE").fetchone()[0]
    assert maxima["WAChatSession"] == db.execute("SELECT COUNT(*) FROM ZWACHATSESSION").fetchone()[0]
    for chat_pk, counter in db.execute("SELECT Z_PK, ZMESSAGECOUNTER FROM ZWACHATSESSION"):
        sorts = [r[0] for r in db.execute("SELECT ZSORT FROM ZWAMESSAGE WHERE ZCHATSESSION = ? ORDER BY ZSORT", (chat_pk,))]
        assert sorts == list(range(1, len(sorts) + 1)) and counter == len(sorts) + 1
    # From WhatsApp 2.23 names and previews are base64 protobuf, never shown.
    assert all(re.fullmatch(r"[A-Za-z0-9+/=]+", r[0]) for r in db.execute("SELECT ZLASTMESSAGETEXT FROM ZWACHATSESSION"))


# ── persona files ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, text, message", [
    ("x.txt", "chat: x\nwith: nobody\n\n== 2021-06-01\n09:00 me: hi\n", "isn't a contact"),
    ("x.txt", "chat: x\nwith: jordan\n\n== 2021-06-01\n09:00 me > jordan: hi\n", "nothing|no earlier message"),
    ("x.txt", "chat: x\nwith: jordan\n\n== 2021-06-01\n09:00 me +👍\n", "nothing to react to"),
    ("x.txt", "chat: x\nwith: jordan\n\n== 2021-06-01\n09:00 me [call:voice missed]\n", "unanswered"),
    ("x.txt", "chat: x\nwith: jordan\n\n== 2021-06-01\n09:00 me [sticker:cat]\n", "unknown \\[sticker\\]"),
    ("x.txt", "chat: x\ntype: broadcast\nname: L\nwith: jordan\n\n== 2021-06-01\n09:00 jordan: hi\n", "only you"),
    ("x.txt", "chat: x\nwith: jordan\n\n== 2021-06-01\n09:00 notice: created\n", "only groups"),
    ("x.txt", "chat: x\nwith: jordan\n\n== 2021-06-01\n09:00 me [document:Nope.pdf]\n", "no whatsapp/documents"),
    ("x.txt", "chat: x\nwith: jordan\n\n== 2021-06-01\n09:00 me [photo:desk]\n", "sent before it was taken"),
    ("x.txt", "chat: x\nwith: jordan, riley\n\n== 2021-06-01\n09:00 me: hi\n", "one other person"),
    ("x.txt", "chat: x\nwith: jordan\n\n== 2021-06-01\n09:00 me [location:91,0 Nowhere]\n", "isn't a place"),
    ("x.txt", "chat: x\nwith: jordan\n\n== 2030-06-01\n09:00 me: hi\n", "outside the persona's period"),
])
def test_bad_whatsapp_files_are_rejected(pack, name, text, message):
    folder = pack / "testpersona" / "whatsapp"
    (folder / name).write_text(text, encoding="utf-8")
    with pytest.raises(PersonaError, match=message):
        load_persona("testpersona", root=pack)


def test_people_file_lines(pack):
    people = pack / "testpersona" / "whatsapp" / "people.txt"
    people.write_text("riley +44 7700 900002 Riley\n", encoding="utf-8")
    with pytest.raises(PersonaError, match="is a contact"):
        load_persona("testpersona", root=pack)
    people.write_text("nobody - Someone\n", encoding="utf-8")
    with pytest.raises(PersonaError, match="isn't a contact"):
        load_persona("testpersona", root=pack)


# ── media helpers ────────────────────────────────────────────────────────────

def _ogg_pages(data: bytes):
    pos = 0
    while pos < len(data):
        assert data[pos:pos + 4] == b"OggS"
        flags, granule, serial, sequence, crc, count = struct.unpack_from("<xBqIIIB", data, pos + 4)
        lacing = data[pos + 27:pos + 27 + count]
        end = pos + 27 + count + sum(lacing)
        page = bytearray(data[pos:end])
        page[22:26] = b"\0\0\0\0"
        yield flags, granule, sequence, crc, writer._ogg_crc(bytes(page)), bytes(data[pos + 27 + count:end])
        pos = end


@pytest.mark.parametrize("seconds", [1, 7, 61])
def test_voice_notes_are_valid_ogg_opus(seconds):
    data = writer.ogg_opus(seconds, serial=1234)
    pages = list(_ogg_pages(data))
    assert all(crc == computed for _, _, _, crc, computed, _ in pages), "page checksums"
    assert [p[2] for p in pages] == list(range(len(pages)))
    assert pages[0][0] == 0x02 and pages[-1][0] == 0x04 and all(p[0] == 0 for p in pages[1:-1])
    head = pages[0][5]
    assert head.startswith(b"OpusHead") and head[9] == 1  # mono
    pre_skip = struct.unpack_from("<H", head, 10)[0]
    assert pages[1][5].startswith(b"OpusTags")
    assert pages[-1][1] - pre_skip == seconds * 48_000


def test_documents_are_well_formed_pdfs():
    data = writer.pdf("A title\nFirst line (with brackets) and a backslash \\ and café")
    xref = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    assert data[xref:xref + 4] == b"xref"
    entries = data[xref:].split(b"\n")[3:9]
    for number, entry in enumerate(entries, 1):
        offset = int(entry[:10])
        assert data[offset:].startswith(f"{number} 0 obj".encode())
    assert b"(First line \\(with brackets\\) and a backslash \\\\ and caf\\351)" in data


def test_map_previews_are_jpegs_with_a_pin():
    import random

    from PIL import Image

    image = Image.open(io.BytesIO(writer.map_preview(random.Random(1))))
    assert image.format == "JPEG" and image.size == (480, 240)
    red = image.getpixel((240, 106))
    assert red[0] > 180 and red[1] < 120, "the pin sits in the middle"
