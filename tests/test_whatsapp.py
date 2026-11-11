"""WhatsApp extraction from ChatStorage.sqlite.

The databases here are built from the table definitions harvested from real
backups (backupgen/schemas/ios-*/ChatStorage.sqlite.json, WhatsApp 2.20 to
2.24), so the queries run against the layout WhatsApp really uses. Every row
is invented: fictional 555 numbers and made-up text. The cases mirror what the
public test backups showed (arrival order that disagrees with dates and row
ids, replies carried in ZMETADATA, reactions in ZRECEIPTINFO, calls as type 59
rows, base64 protobuf in ZPUSHNAME, LIDs) without copying any of their data.
"""

import base64
import os
import plistlib
import re
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
from backupgen.sqlitegen import build  # noqa: E402
from tests.test_read_only_backup import _read_only, _snapshot  # noqa: E402
from ios_backup_core.backup import BackupReader, open_local_backup  # noqa: E402
from ios_backup_core.extractors.whatsapp import SHARED_DOMAIN, WhatsAppExtractor  # noqa: E402
from ios_backup_core.protobuf import (  # noqa: E402
    ProtobufError,
    all_fields,
    field_int,
    field_text,
    first_field,
    parse_fields,
    sub_message,
)

VERSIONS = sorted(
    (p.parent.name.removeprefix("ios-") for p in SCHEMAS_DIR.glob("ios-*/ChatStorage.sqlite.json")),
    key=lambda v: tuple(int(x) for x in v.split(".")),
)

OWNER, RILEY, CASEY = "12025550100", "12025550101", "12025550102"
OWNER_LID, OWNER_LID_OLD, CASEY_LID, STRANGER_LID = "880000000000001", "880000000000002", "880000000000003", "880000000000009"
GROUP_JID = "120363000000000001@g.us"
CONTACTS = {"+12025550101": "Riley Sample", "2025550101": "Riley Sample", "0000000001": "Wrong Person"}
PLIST = "Library/Preferences/group.net.whatsapp.WhatsApp.shared.plist"
IMAGE_BYTES = b"\xff\xd8\xff\xe0" + b"fictional jpeg" * 40
THUMB_BYTES = b"\xff\xd8\xff\xdb" + b"thumb" * 20


def jid(number: str) -> str:
    return f"{number}@s.whatsapp.net"


def apple(iso: str) -> float:
    dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    return (dt - datetime(2001, 1, 1, tzinfo=timezone.utc)).total_seconds()


T0 = apple("2024-03-01T12:00:00")


# ── protobuf encoding for fixtures ───────────────────────────────────────────

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def pb(*fields) -> bytes:
    """Encode ``(number, value)`` pairs: ints as varints, str/bytes as length-delimited, lists as nested messages."""
    out = bytearray()
    for number, value in fields:
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int):
            out += _varint(number << 3) + _varint(value)
            continue
        if isinstance(value, str):
            value = value.encode("utf-8")
        elif isinstance(value, (list, tuple)):
            value = pb(*value)
        out += _varint(number << 3 | 2) + _varint(len(value)) + value
    return bytes(out)


def call_meta(video: bool, outcome: int, seconds: int) -> bytes:
    return pb((87, [(1, [(1, video), (2, outcome), (3, seconds), (4, 0), (5, [(1, jid(RILEY)), (2, 0)])]),
                    (2, 0), (3, 1), (4, 0)]))


def unix_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


# ── fake backup ──────────────────────────────────────────────────────────────

class FakeBackup:
    """Accessor stub keyed by (domain, relative path). ``unreadable`` entries are listed but can't be extracted."""

    encrypted = True
    udid = "00008120-0000000000000000"

    def __init__(self, files=None, unreadable=()):
        self.files = dict(files or {})
        self.unreadable = set(unreadable)

    def get_file(self, relative_path, domain="HomeDomain"):
        return self.files.get((domain, relative_path))

    def list_files(self, domain=None, path_like=None):
        entries = [*self.files, *self.unreadable]
        if domain:
            entries = [e for e in entries if e[0] == domain]
        if path_like:
            pattern = ".*".join(re.escape(p) for p in path_like.split("%"))
            entries = [e for e in entries if re.fullmatch(pattern, e[1], re.IGNORECASE)]
        return [{"hash": "", "domain": d, "path": p} for d, p in entries]


def has_schema(version: str, database: str) -> bool:
    return (SCHEMAS_DIR / f"ios-{version}" / f"{database}.json").is_file()


class Store:
    """The standard scenario for one WhatsApp schema version."""

    def __init__(self, tmp_path: Path, version: str):
        self.version = version
        self.has_lid = has_schema(version, "LID.sqlite")
        self.has_contacts_v2 = has_schema(version, "ContactsV2.sqlite")
        self.casey_jid = f"{CASEY_LID}@lid" if self.has_lid else jid(CASEY)
        self.casey_name = "Casey Example" if self.has_contacts_v2 else "Casey"
        self.dir = tmp_path
        self.files = {}
        self.image_path = f"Media/{jid(RILEY)}/a/b/abcd-image.jpg"
        self.thumb_path = f"Media/{jid(RILEY)}/a/b/abcd-image.thumb"
        self.map_thumb = f"Media/{jid(OWNER)}/c/d/map.thumb"
        self.missing_path = f"Media/{jid(RILEY)}/e/f/missing.jpg"
        build(tmp_path / "ChatStorage.sqlite", version, "ChatStorage.sqlite", self._populate)
        self.files[(SHARED_DOMAIN, "ChatStorage.sqlite")] = str(tmp_path / "ChatStorage.sqlite")
        if self.has_lid:
            build(tmp_path / "LID.sqlite", version, "LID.sqlite", self._populate_lid)
            self.files[(SHARED_DOMAIN, "LID.sqlite")] = str(tmp_path / "LID.sqlite")
        if self.has_contacts_v2:
            build(tmp_path / "ContactsV2.sqlite", version, "ContactsV2.sqlite", self._populate_contacts)
            self.files[(SHARED_DOMAIN, "ContactsV2.sqlite")] = str(tmp_path / "ContactsV2.sqlite")
        prefs = {"OwnJabberID": jid(OWNER), "lid": f"{OWNER_LID}@lid", "FullUserName": "Pat Owner"}
        self._add(PLIST, plistlib.dumps(prefs, fmt=plistlib.FMT_BINARY))
        self._add("Message/" + self.image_path, IMAGE_BYTES)
        self._add("Message/" + self.thumb_path, THUMB_BYTES)
        self._add("Message/" + self.map_thumb, THUMB_BYTES)
        self.backup = FakeBackup(self.files)

    def _add(self, rel: str, data: bytes) -> None:
        local = self.dir / ("f%03d" % len(self.files))
        local.write_bytes(data)
        self.files[(SHARED_DOMAIN, rel)] = str(local)

    # Row ids are deliberately not in ZSORT order, and dates sometimes disagree with ZSORT.
    def _populate(self, db) -> None:
        chats, msgs, media, info = (db.table(n) for n in ("ZWACHATSESSION", "ZWAMESSAGE", "ZWAMEDIAITEM", "ZWAMESSAGEINFO"))
        members, groups, push = db.table("ZWAGROUPMEMBER"), db.table("ZWAGROUPINFO"), db.table("ZWAPROFILEPUSHNAME")
        protobuf_text = base64.b64encode(pb((1, 1709294400), (4, 0))).decode()

        def chat(pk, contact_jid, session_type, partner="", counter=0, archived=0):
            chats.insert({"Z_PK": pk, "Z_ENT": 4, "ZCONTACTJID": contact_jid, "ZSESSIONTYPE": session_type,
                          "ZPARTNERNAME": partner, "ZMESSAGECOUNTER": counter, "ZARCHIVED": archived,
                          "ZLASTMESSAGETEXT": protobuf_text, "ZHIDDEN": 0, "ZREMOVED": 0})

        def message(pk, chat_pk, sort, mtype, date, *, event=None, from_me=False, text=None, frm=None, to=None,
                    sent=None, member=None, pushname=None, stanza=None, media_pk=None, info_pk=None):
            msgs.insert({"Z_PK": pk, "Z_ENT": 9, "ZCHATSESSION": chat_pk, "ZSORT": sort, "ZMESSAGETYPE": mtype,
                         "ZGROUPEVENTTYPE": event if event is not None else (0 if from_me else 2),
                         "ZISFROMME": int(from_me), "ZMESSAGEDATE": date, "ZSENTDATE": sent, "ZTEXT": text,
                         "ZFROMJID": frm, "ZTOJID": to, "ZGROUPMEMBER": member, "ZPUSHNAME": pushname,
                         "ZSTANZAID": stanza or f"3A{pk:018d}", "ZMEDIAITEM": media_pk, "ZMESSAGEINFO": info_pk,
                         "ZMESSAGESTATUS": 8, "ZFLAGS": 0x1000000})

        def media_row(pk, msg_pk, **cols):
            media.insert({"Z_PK": pk, "Z_ENT": 8, "ZMESSAGE": msg_pk, **cols})

        def receipt(pk, msg_pk, blob):
            info.insert({"Z_PK": pk, "Z_ENT": 11, "ZMESSAGE": msg_pk, "ZRECEIPTINFO": blob})

        R = jid(RILEY)
        chat(1, R, 0, partner="Riley WA", counter=99)
        message(10, 1, 1, 10, T0, event=2, frm=R, to=jid(OWNER))
        message(12, 1, 2, 0, T0 + 60, frm=R, text="Hello from Riley", sent=T0 + 65, stanza="3ARILEYHELLO0000001", info_pk=1)
        receipt(1, 12, pb((4, 2), (7, [(2, [(1, "3AOWNREACT"), (2, "❤️"), (3, unix_ms("2024-03-01T12:01:30")), (4, 1)]),
                                       (1, [(1, "WITHDRAWN"), (2, R), (3, ""), (4, unix_ms("2024-03-01T12:01:40"))])])))
        message(11, 1, 3, 0, T0 + 120, from_me=True, text="Reply to you", to=R, media_pk=1, info_pk=2)
        media_row(1, 11, ZMETADATA=pb((5, "3ARILEYHELLO0000001"), (6, R), (19, [(1, "Hello from Riley")])))
        receipt(2, 11, pb((2, [(1, b"\x81\x12\x02\x55\x50\x10\x1f"), (5, 3)]), (3, 1709294530),
                          (7, [(1, [(1, "A" * 32), (2, R), (3, "🙏"), (4, unix_ms("2024-03-01T12:03:00")), (5, 0)])])))
        message(13, 1, 4, 1, T0 + 180, frm=R, sent=63113904000, media_pk=2)
        media_row(2, 13, ZMEDIALOCALPATH=self.image_path, ZXMPPTHUMBPATH=self.thumb_path, ZLATITUDE=1600.0,
                  ZLONGITUDE=1200.0, ZVCARDSTRING="image/jpeg", ZVCARDNAME="ignored-hash", ZFILESIZE=len(IMAGE_BYTES),
                  ZTITLE="Caption here", ZMEDIAORIGIN=0)
        message(14, 1, 5, 59, T0 + 240, frm=R, media_pk=3)
        media_row(3, 14, ZMETADATA=call_meta(True, 0, 125))
        message(15, 1, 6, 59, T0 + 300, from_me=True, to=R, media_pk=4)
        media_row(4, 15, ZMETADATA=call_meta(False, 0, 0))
        message(16, 1, 7, 59, T0 + 360, frm=R, media_pk=5)
        media_row(5, 16, ZMETADATA=call_meta(False, 1, 0))
        message(17, 1, 8, 5, T0 + 420, from_me=True, to=R, media_pk=6)
        media_row(6, 17, ZLATITUDE=45.52, ZLONGITUDE=-122.68, ZMEDIAORIGIN=0, ZXMPPTHUMBPATH=self.map_thumb)
        message(18, 1, 9, 5, T0 + 480, frm=R, media_pk=7)
        media_row(7, 18, ZLATITUDE=45.53, ZLONGITUDE=-122.69, ZMEDIAORIGIN=1, ZXMPPTHUMBPATH="Media/gone/map.thumb",
                  ZAUTHORNAME="1709294880123456", ZMEDIAURLDATE=-63114076800)
        message(19, 1, 10, 99, T0 + 540, frm=R, text="A message type from the future")
        message(20, 1, 11, 0, T0 + 600, from_me=True, to=R, text="Quoting a deleted one", media_pk=8)
        media_row(8, 20, ZMETADATA=pb((5, "3AGONE00000000000000"), (19, [(1, "old words")])))
        message(21, 1, 12, 1, int(T0 + 660), from_me=True, to=R, media_pk=9)
        media_row(9, 21, ZMEDIALOCALPATH=self.missing_path, ZVCARDSTRING="image/jpeg", ZFILESIZE=5000,
                  ZLATITUDE=10.0, ZLONGITUDE=20.0)
        # ZSORT 13 is missing: a message deleted "for me" leaves no row.
        message(22, 1, 14, 14, T0 + 720, frm=R)
        message(23, 1, 15, 0, T0 + 700, frm=R, text="last words 100% _done_", sent=T0 + 730)

        chat(2, GROUP_JID, 1, partner="Weekend Plans", counter=10)
        groups.insert({"Z_PK": 1, "Z_ENT": 5, "ZCHATSESSION": 2, "ZCREATORJID": jid(OWNER), "ZCREATIONDATE": T0 - 86400})
        members.insert({"Z_PK": 1, "Z_ENT": 6, "ZCHATSESSION": 2, "ZMEMBERJID": self.casey_jid, "ZCONTACTNAME": ""})
        members.insert({"Z_PK": 2, "Z_ENT": 6, "ZCHATSESSION": 2, "ZMEMBERJID": jid(OWNER), "ZCONTACTNAME": ""})
        push.insert({"Z_PK": 1, "Z_ENT": 13, "ZJID": jid(CASEY), "ZPUSHNAME": "Casey"})
        message(30, 2, 1, 10, T0 - 86400, event=2, frm=GROUP_JID, to=jid(OWNER))
        message(31, 2, 2, 6, T0 - 86400, event=12, frm=GROUP_JID, to=jid(OWNER))
        message(32, 2, 3, 6, T0 - 86399, event=2, frm=GROUP_JID, to=jid(OWNER), member=1)
        message(33, 2, 4, 0, T0 - 80000, frm=GROUP_JID, member=1, text="Hi all")
        message(34, 2, 5, 0, T0 - 79000, frm=GROUP_JID, pushname="Drew", text="Drew here")
        message(35, 2, 6, 0, T0 - 78000, frm=GROUP_JID, pushname=protobuf_text, text="Who am I")
        message(36, 2, 7, 6, T0 - 77000, event=1, frm=GROUP_JID, to=jid(OWNER),
                text='{"subject": "New Name", "author": "%s"}' % jid(CASEY))
        message(37, 2, 8, 10, T0 - 76000, event=56, frm=GROUP_JID, to=jid(OWNER))
        message(38, 2, 9, 0, T0 - 75000, frm=f"{CASEY}:3@s.whatsapp.net", text="From my laptop")

        chat(3, "0@status", 3, partner="WhatsApp")
        message(40, 3, 1, 0, T0 + 900, frm=jid(RILEY), text="status secret words")
        chat(4, "status@broadcast", 2)
        message(41, 4, 1, 0, T0 + 901, from_me=True, text="status secret words")
        chat(5, "1700000000@broadcast", 2, partner="Family list", archived=1)
        message(50, 5, 1, 0, T0 - 200000, from_me=True, text="Hello family")
        chat(6, jid("12025550109"), 0, partner="Nobody")  # no messages: not listed
        chat(7, f"{STRANGER_LID}@lid", 0)
        message(70, 7, 1, 0, T0 - 300000, frm=f"{STRANGER_LID}@lid", text="Who is this")

    def _populate_lid(self, db) -> None:
        pairs = db.table("ZWAPHONENUMBERLIDPAIR")
        pairs.insert({"Z_PK": 4, "Z_ENT": 3, "ZLID": CASEY_LID, "ZPHONENUMBER": "12025550199", "ZTIMESTAMP": 0.5})
        pairs.insert({"Z_PK": 1, "Z_ENT": 3, "ZLID": "111", "ZPHONENUMBER": CASEY, "ZTIMESTAMP": 1.0})
        pairs.insert({"Z_PK": 2, "Z_ENT": 3, "ZLID": CASEY_LID, "ZPHONENUMBER": CASEY, "ZTIMESTAMP": 2.0})
        pairs.insert({"Z_PK": 3, "Z_ENT": 3, "ZLID": OWNER_LID_OLD, "ZPHONENUMBER": OWNER, "ZTIMESTAMP": 3.0})

    def _populate_contacts(self, db) -> None:
        whatsapp_id = jid(CASEY) if self.version.startswith("15") else CASEY
        db.table("ZWAADDRESSBOOKCONTACT").insert(
            {"Z_PK": 1, "Z_ENT": 1, "ZWHATSAPPID": whatsapp_id, "ZFULLNAME": "Casey Example", "ZPHONENUMBER": "(202) 555-0102"})


@pytest.fixture(params=VERSIONS, ids=lambda v: f"ios-{v}")
def store(request, tmp_path):
    return Store(tmp_path, request.param)


@pytest.fixture
def latest(tmp_path):
    return Store(tmp_path, VERSIONS[-1])


def all_messages(store, chat_id):
    return WhatsAppExtractor().get_messages(store.backup, chat_id, CONTACTS, limit=1000)["messages"]


def by_id(messages):
    return {m["message_id"]: m for m in messages}


# ── chats ────────────────────────────────────────────────────────────────────

def test_schemas_cover_whatsapp_2_20_to_2_24():
    assert VERSIONS[0] == "13.3.1" and len(VERSIONS) >= 5


class TestConversations:
    def test_lists_chats_with_messages_newest_first_and_hides_status(self, store):
        result = WhatsAppExtractor().list_conversations(store.backup, CONTACTS)
        assert "error" not in result and "empty_reason" not in result
        assert [c["chat_id"] for c in result["conversations"]] == [1, 2, 5, 7]

    def test_keys_extend_the_messages_shape(self, latest):
        conv = WhatsAppExtractor().list_conversations(latest.backup, CONTACTS)["conversations"][0]
        assert {"chat_id", "chat_identifier", "display_name", "service", "message_count", "last_message_date",
                "last_message_preview", "is_group"} <= set(conv)
        assert conv["service"] == "WhatsApp"
        assert conv["chat_identifier"] == conv["chat_jid"] == jid(RILEY)

    def test_counts_rows_rather_than_trusting_the_counter(self, store):
        convs = {c["chat_id"]: c for c in WhatsAppExtractor().list_conversations(store.backup, CONTACTS)["conversations"]}
        assert convs[1]["message_count"] == 14
        assert convs[2]["message_count"] == 9

    def test_names_types_and_flags(self, store):
        convs = {c["chat_id"]: c for c in WhatsAppExtractor().list_conversations(store.backup, CONTACTS)["conversations"]}
        assert convs[1]["display_name"] == "Riley Sample"  # the address book beats WhatsApp's own name
        assert (convs[1]["chat_type"], convs[1]["is_group"]) == ("individual", False)
        assert convs[2]["display_name"] == "Weekend Plans"  # never "Wrong Person": group ids aren't phone numbers
        assert (convs[2]["chat_type"], convs[2]["is_group"]) == ("group", True)
        assert (convs[5]["display_name"], convs[5]["chat_type"], convs[5]["archived"]) == ("Family list", "broadcast", True)
        assert convs[7]["display_name"] == "Unknown contact"

    def test_handle_is_the_other_persons_number(self, store):
        convs = {c["chat_id"]: c for c in WhatsAppExtractor().list_conversations(store.backup, CONTACTS)["conversations"]}
        assert convs[1]["handle"] == "+" + RILEY
        assert convs[2]["handle"] == convs[5]["handle"] == ""  # a group or a list is nobody's number
        assert convs[7]["handle"] == ""  # a LID nothing maps to a number

    def test_handle_of_a_chat_kept_under_a_lid(self, tmp_path):
        version = next(v for v in VERSIONS if has_schema(v, "LID.sqlite"))
        store = Store(tmp_path, version)
        conn = sqlite3.connect(store.files[(SHARED_DOMAIN, "ChatStorage.sqlite")])
        conn.execute("UPDATE ZWACHATSESSION SET ZCONTACTJID = ? WHERE Z_PK = 7", (f"{CASEY_LID}@lid",))
        conn.commit()
        conn.close()
        convs = {c["chat_id"]: c for c in WhatsAppExtractor().list_conversations(store.backup, CONTACTS)["conversations"]}
        assert convs[7]["handle"] == "+" + CASEY

    def test_preview_comes_from_the_last_message_by_arrival(self, store):
        convs = {c["chat_id"]: c for c in WhatsAppExtractor().list_conversations(store.backup, CONTACTS)["conversations"]}
        assert convs[1]["last_message_preview"] == "last words 100% _done_"
        assert convs[1]["last_message_date"].startswith("2024-03-01T12:11:40")  # the preview's own time, not the latest
        assert convs[2]["last_message_preview"] == "From my laptop"

    def test_has_whatsapp_counts_listable_chats(self, store):
        assert WhatsAppExtractor().has_whatsapp(store.backup) == {"has_whatsapp": True, "chats": 4, "messages": 25}

    def test_backup_reader(self, latest):
        reader = BackupReader(latest.backup)
        assert [c["chat_id"] for c in reader.whatsapp()["conversations"]] == [1, 2, 5, 7]
        assert reader.whatsapp(chat_id=1, limit=2)["total"] == 14


# ── messages ─────────────────────────────────────────────────────────────────

class TestMessages:
    def test_arrival_order_not_row_id_or_date(self, store):
        messages = all_messages(store, 1)
        assert [m["message_id"] for m in messages] == [10, 12, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
        assert [m["sort"] for m in messages] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15]

    def test_every_message_has_the_messages_keys(self, store):
        keys = {"message_id", "text", "message_type", "event_text", "link_preview", "date", "is_from_me", "sender",
                "sender_handle", "has_attachments", "attachments", "is_reaction"}
        for m in all_messages(store, 1) + all_messages(store, 2):
            assert keys <= set(m)
            assert m["is_reaction"] is False

    def test_paging_counts_back_from_the_newest(self, store):
        wa = WhatsAppExtractor()
        first = wa.get_messages(store.backup, 1, CONTACTS, offset=0, limit=5)
        assert (first["total"], first["offset"], first["limit"], first["next_offset"]) == (14, 0, 5, 5)
        assert [m["sort"] for m in first["messages"]] == [10, 11, 12, 14, 15]
        second = wa.get_messages(store.backup, 1, CONTACTS, offset=5, limit=5)
        assert [m["sort"] for m in second["messages"]] == [5, 6, 7, 8, 9]
        last = wa.get_messages(store.backup, 1, CONTACTS, offset=10, limit=5)
        assert [m["sort"] for m in last["messages"]] == [1, 2, 3, 4]
        assert last["next_offset"] == 14

    def test_date_filter_applies_inside_the_paging(self, store):
        wa = WhatsAppExtractor()
        result = wa.get_messages(store.backup, 1, CONTACTS, limit=2,
                                 date_from="2024-03-01T12:04:00+00:00", date_to="2024-03-01T12:08:00+00:00")
        assert result["total"] == 5
        assert [m["message_id"] for m in result["messages"]] == [17, 18]
        empty = wa.get_messages(store.backup, 1, CONTACTS, date_from="1990-01-01", date_to="1990-01-02")
        assert (empty["messages"], empty["total"]) == ([], 0)

    def test_text_senders_and_received_time(self, store):
        m = by_id(all_messages(store, 1))
        assert (m[12]["text"], m[12]["sender"], m[12]["sender_handle"], m[12]["is_from_me"]) == (
            "Hello from Riley", "Riley Sample", "+12025550101", False)
        assert m[12]["received_date"].startswith("2024-03-01T12:01:05")
        assert (m[11]["sender"], m[11]["sender_handle"], m[11]["is_from_me"]) == ("me", "+12025550100", True)
        assert m[13]["received_date"] is None  # a sentinel date is no date

    def test_reply_to_a_message_in_the_chat(self, store):
        m = by_id(all_messages(store, 1))
        assert m[11]["quoted"] == {"message_id": 12, "text": "Hello from Riley", "message_type": "text",
                                   "is_from_me": False, "sender": "Riley Sample"}
        assert m[11]["attachments"] == []  # the media row only carried the quote

    def test_reply_to_a_message_that_is_gone_uses_the_copy(self, store):
        quoted = by_id(all_messages(store, 1))[20]["quoted"]
        assert quoted == {"message_id": None, "text": "old words", "message_type": "text", "is_from_me": True, "sender": "You"}

    def test_reactions_sit_on_the_message_they_react_to(self, store):
        m = by_id(all_messages(store, 1))
        assert [(r["emoji"], r["sender"], r["is_from_me"]) for r in m[12]["reactions"]] == [("❤️", "You", True)]
        assert m[12]["reactions"][0]["date"].startswith("2024-03-01T12:01:30")
        assert [(r["emoji"], r["sender"], r["sender_handle"]) for r in m[11]["reactions"]] == [
            ("🙏", "Riley Sample", "+12025550101")]

    def test_image(self, store):
        m = by_id(all_messages(store, 1))[13]
        assert (m["message_type"], m["text"], m["has_attachments"]) == ("image", "Caption here", True)
        att = m["attachments"][0]
        assert att["filename"] == "Message/" + store.image_path
        assert (att["mime_type"], att["total_bytes"], att["available"]) == ("image/jpeg", len(IMAGE_BYTES), True)
        assert (att["width"], att["height"]) == (1200, 1600)  # ZLONGITUDE is width, ZLATITUDE height
        assert att["thumbnail_path"] == "Message/" + store.thumb_path

    def test_media_that_never_made_it_into_the_backup(self, store):
        m = by_id(all_messages(store, 1))[21]
        att = m["attachments"][0]
        assert m["has_attachments"] and att["available"] is False and att["thumbnail_path"] is None
        assert att["filename"] == "Message/" + store.missing_path
        assert m["date"].startswith("2024-03-01T12:11:00")  # an integer ZMESSAGEDATE reads the same

    def test_calls(self, store):
        m = by_id(all_messages(store, 1))
        assert m[14]["call"] == {"is_video": True, "duration": 125, "outcome": "answered", "direction": "incoming"}
        assert (m[14]["message_type"], m[14]["event_text"]) == ("call", "Incoming video call · 2:05")
        assert m[15]["call"]["outcome"] == "no_answer" and m[15]["event_text"] == "Unanswered voice call"
        assert m[16]["call"]["outcome"] == "missed" and m[16]["event_text"] == "Missed voice call"
        assert all(m[i]["attachments"] == [] for i in (14, 15, 16))

    def test_locations(self, store):
        m = by_id(all_messages(store, 1))
        assert (m[17]["message_type"], m[17]["event_text"]) == ("shared_location", "Location")
        assert m[17]["location"] == {"latitude": 45.52, "longitude": -122.68, "live": False, "place_name": None,
                                     "thumbnail_path": "Message/" + store.map_thumb, "attachment_id": 6}
        assert (m[18]["message_type"], m[18]["event_text"]) == ("live_location", "Live location")
        assert m[18]["location"]["live"] is True and m[18]["location"]["thumbnail_path"] is None
        thumb = WhatsAppExtractor().get_attachment(store.backup, m[17]["location"]["attachment_id"], thumbnail=True)
        assert Path(thumb["path"]).read_bytes() == THUMB_BYTES
        assert m[18]["sender"] == "Riley Sample"  # ZAUTHORNAME is not an author

    def test_unknown_and_deleted_messages_are_kept(self, store):
        m = by_id(all_messages(store, 1))
        assert (m[19]["message_type"], m[19]["text"]) == ("unknown", "A message type from the future")
        assert (m[22]["message_type"], m[22]["event_text"], m[22]["text"]) == ("deleted", "This message was deleted", "")

    def test_system_messages(self, store):
        m = by_id(all_messages(store, 2))
        assert m[30]["event_text"] == "Messages and calls are end-to-end encrypted."
        assert m[31]["event_text"] == "You created the group"
        assert m[32]["event_text"] == f"{store.casey_name} was added"
        assert m[36]["event_text"] == "The group name was changed to “New Name”"
        assert m[37]["event_text"] == "WhatsApp notice"  # 10/56: sources disagree on what it says
        for pk in (30, 31, 32, 36, 37):
            assert (m[pk]["message_type"], m[pk]["is_from_me"], m[pk]["sender"], m[pk]["text"]) == ("system", False, "", "")
        assert (m[30]["event_code"], m[37]["event_code"]) == ("10/2", "10/56")

    def test_group_senders(self, store):
        m = by_id(all_messages(store, 2))
        assert (m[33]["sender"], m[33]["sender_jid"]) == (store.casey_name, store.casey_jid)  # via ZGROUPMEMBER (and LID)
        assert m[33]["sender_handle"] == "+12025550102"
        assert m[34]["sender"] == "Drew"  # a plain push name
        assert m[35]["sender"] == "Unknown member"  # ZPUSHNAME is protobuf here, never a name
        assert m[38]["sender"] == store.casey_name  # ":3" device suffix stripped
        assert all("Wrong Person" not in (msg["sender"], msg["event_text"]) for msg in m.values())

    def test_unmapped_lid(self, store):
        (msg,) = all_messages(store, 7)
        assert msg["sender"] == "Unknown member"

    def test_unknown_chat_is_an_empty_page(self, store):
        result = WhatsAppExtractor().get_messages(store.backup, 999, CONTACTS)
        assert (result["messages"], result["total"]) == ([], 0)
        result = WhatsAppExtractor().get_messages(store.backup, 3, CONTACTS)  # status updates aren't a chat
        assert result["messages"] == []


class TestOwnerAndLids:
    def test_owner_lid_from_the_plist_and_from_lid_pairs(self, tmp_path):
        version = next(v for v in VERSIONS if has_schema(v, "LID.sqlite"))
        store = Store(tmp_path, version)
        wa = WhatsAppExtractor()
        conn = sqlite3.connect(store.files[(SHARED_DOMAIN, "ChatStorage.sqlite")])
        conn.execute("UPDATE ZWAMESSAGE SET ZFROMJID = ?, ZGROUPMEMBER = NULL WHERE Z_PK = 33", (f"{OWNER_LID}@lid",))
        conn.execute("UPDATE ZWAMESSAGE SET ZFROMJID = ? WHERE Z_PK = 34", (f"{OWNER_LID_OLD}@lid",))
        conn.commit()
        conn.close()
        m = by_id(wa.get_messages(store.backup, 2, CONTACTS, limit=100)["messages"])
        assert m[33]["sender"] == m[34]["sender"] == "You"

    def test_owner_from_system_rows_when_the_plist_is_missing(self, latest):
        del latest.backup.files[(SHARED_DOMAIN, PLIST)]
        m = by_id(all_messages(latest, 1))
        assert m[11]["sender_handle"] == "+12025550100"
        group = by_id(all_messages(latest, 2))
        assert group[31]["event_text"] == "You created the group"


# ── search, offsets, files ───────────────────────────────────────────────────

class TestSearch:
    def test_text_and_captions(self, store):
        wa = WhatsAppExtractor()
        hits = wa.search_messages(store.backup, "riley", CONTACTS)["results"]
        assert [(h["message_id"], h["sender"], h["chat_id"]) for h in hits] == [(12, "Riley Sample", 1)]
        caption = wa.search_messages(store.backup, "caption", CONTACTS)["results"]
        assert [(h["message_id"], h["text"], h["chat_name"]) for h in caption] == [(13, "Caption here", "Riley Sample")]

    def test_wildcards_are_literal(self, store):
        wa = WhatsAppExtractor()
        assert [h["message_id"] for h in wa.search_messages(store.backup, "100%", CONTACTS)["results"]] == [23]
        assert [h["message_id"] for h in wa.search_messages(store.backup, "_done_", CONTACTS)["results"]] == [23]
        assert wa.search_messages(store.backup, "o_d", CONTACTS)["results"] == []

    def test_skips_status_updates_and_system_rows(self, store):
        wa = WhatsAppExtractor()
        assert wa.search_messages(store.backup, "status secret", CONTACTS)["results"] == []
        assert wa.search_messages(store.backup, "subject", CONTACTS)["results"] == []

    def test_filters_and_shape(self, store):
        wa = WhatsAppExtractor()
        hits = wa.search_messages(store.backup, "Hi", CONTACTS, chat_id=2)["results"]
        assert [h["message_id"] for h in hits] == [33]
        assert set(hits[0]) == {"message_id", "text", "date", "is_from_me", "sender", "chat_id", "chat_name"}
        assert wa.search_messages(store.backup, "  ", CONTACTS) == {"results": [], "query": "  "}
        recent = wa.search_messages(store.backup, "Hello", CONTACTS, date_from="2024-03-01T12:00:30+00:00")
        assert [h["message_id"] for h in recent["results"]] == [12]
        older = wa.search_messages(store.backup, "Hello", CONTACTS, date_to="2024-02-29")
        assert [h["message_id"] for h in older["results"]] == [50]


class TestOffsetsAndFiles:
    def test_offset_matches_the_paging(self, store):
        wa = WhatsAppExtractor()
        messages = all_messages(store, 1)
        for index, msg in enumerate(messages):
            result = wa.get_message_offset(store.backup, 1, msg["message_id"])
            assert result == {"offset": len(messages) - 1 - index, "found": True}
        assert wa.get_message_offset(store.backup, 2, 12) == {"offset": 0, "found": False}

    def test_get_attachment(self, store):
        wa = WhatsAppExtractor()
        att = wa.get_attachment(store.backup, 2)
        assert Path(att["path"]).read_bytes() == IMAGE_BYTES
        assert (att["mime_type"], att["filename"]) == ("image/jpeg", "abcd-image.jpg")
        thumb = wa.get_attachment(store.backup, 2, thumbnail=True)
        assert Path(thumb["path"]).read_bytes() == THUMB_BYTES and thumb["mime_type"] == "image/jpeg"
        assert wa.get_attachment(store.backup, 9) == {"error": "Attachment file not found in backup"}
        assert wa.get_attachment(store.backup, 3) == {"error": "Attachment file not found in backup"}  # a call
        assert wa.get_attachment(store.backup, 10**9) == {"error": "Attachment not found"}


# ── empty states ─────────────────────────────────────────────────────────────

def _chat_db_only(tmp_path, version, populate):
    path = tmp_path / "ChatStorage.sqlite"
    build(path, version, "ChatStorage.sqlite", populate)
    return FakeBackup({(SHARED_DOMAIN, "ChatStorage.sqlite"): str(path)})


class TestEmptyStates:
    def test_not_in_backup_is_not_an_error(self):
        wa, backup = WhatsAppExtractor(), FakeBackup()
        assert wa.list_conversations(backup, CONTACTS) == {"conversations": [], "empty_reason": "not_in_backup"}
        page = wa.get_messages(backup, 1, CONTACTS, offset=0, limit=50)
        assert page == {"messages": [], "total": 0, "next_offset": 0, "offset": 0, "limit": 50,
                        "empty_reason": "not_in_backup"}
        assert wa.search_messages(backup, "x", CONTACTS) == {"results": [], "query": "x", "empty_reason": "not_in_backup"}
        assert wa.has_whatsapp(backup) == {"has_whatsapp": False, "chats": 0, "messages": 0,
                                           "empty_reason": "not_in_backup"}
        assert wa.get_message_offset(backup, 1, 1) == {"offset": 0, "found": False}
        assert "error" in wa.get_attachment(backup, 1)

    def test_listed_but_unreadable_is_an_error(self):
        wa = WhatsAppExtractor()
        backup = FakeBackup(unreadable=[(SHARED_DOMAIN, "ChatStorage.sqlite")])
        result = wa.list_conversations(backup, CONTACTS)
        assert result["conversations"] == [] and "empty_reason" not in result
        assert "could not be read" in result["error"]
        assert wa.has_whatsapp(backup)["error"] == result["error"]

    def test_not_a_database_is_an_error(self, tmp_path):
        bad = tmp_path / "ChatStorage.sqlite"
        bad.write_bytes(b"not a database at all" * 100)
        result = WhatsAppExtractor().list_conversations(FakeBackup({(SHARED_DOMAIN, "ChatStorage.sqlite"): str(bad)}), CONTACTS)
        assert result["conversations"] == [] and "empty_reason" not in result and result["error"]

    def test_unknown_layout(self, tmp_path):
        path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE ZSOMETHINGELSE (Z_PK INTEGER PRIMARY KEY)")
        conn.close()
        result = WhatsAppExtractor().list_conversations(FakeBackup({(SHARED_DOMAIN, "ChatStorage.sqlite"): str(path)}), CONTACTS)
        assert result["empty_reason"] == "unsupported_format" and result["error"]

    @pytest.mark.parametrize("version", VERSIONS, ids=lambda v: f"ios-{v}")
    def test_no_chats_or_only_status_updates(self, tmp_path, version):
        def only_status(db):
            db.table("ZWACHATSESSION").insert({"Z_PK": 1, "Z_ENT": 4, "ZCONTACTJID": "0@status", "ZSESSIONTYPE": 3})
            db.table("ZWAMESSAGE").insert({"Z_PK": 1, "Z_ENT": 9, "ZCHATSESSION": 1, "ZSORT": 1, "ZMESSAGETYPE": 0,
                                           "ZMESSAGEDATE": T0, "ZTEXT": "a status"})
        for populate, name in ((lambda db: None, "empty"), (only_status, "status")):
            sub = tmp_path / name
            sub.mkdir()
            backup = _chat_db_only(sub, version, populate)
            result = WhatsAppExtractor().list_conversations(backup, CONTACTS)
            assert result == {"conversations": [], "empty_reason": "no_records"}
            assert WhatsAppExtractor().has_whatsapp(backup)["has_whatsapp"] is False


# ── a real backup on disk ────────────────────────────────────────────────────

@pytest.mark.parametrize("password", [None, "secret"])
def test_through_a_real_backup(tmp_path, password):
    (tmp_path / "parts").mkdir()
    store = Store(tmp_path / "parts", VERSIONS[-1])
    device = Device(name="Test iPhone", product_type="iPhone12,1", product_name="iPhone 11",
                    ios="17.3", build="21D50", phone_number="+1 202 555 0100")
    writer = BackupWriter(tmp_path / "out", device, datetime(2024, 3, 2, tzinfo=timezone.utc), "17.3",
                          password=password, seed=1)
    for (domain, rel), local in store.files.items():
        writer.add_file(domain, rel, Path(local).read_bytes())
    accessor = open_local_backup(str(writer.finalize()), password=password)
    wa = WhatsAppExtractor()
    assert [c["chat_id"] for c in wa.list_conversations(accessor, CONTACTS)["conversations"]] == [1, 2, 5, 7]
    m = by_id(wa.get_messages(accessor, 1, CONTACTS, limit=100)["messages"])
    assert m[13]["attachments"][0]["available"] is True
    assert m[21]["attachments"][0]["available"] is False
    assert Path(wa.get_attachment(accessor, 2)["path"]).read_bytes() == IMAGE_BYTES
    assert by_id(wa.get_messages(accessor, 2, CONTACTS, limit=100)["messages"])[33]["sender"] == store.casey_name


# ── protobuf decoding ────────────────────────────────────────────────────────

class TestParseFields:
    def test_fields_in_order_with_numbers_and_types(self):
        blob = pb((1, 150), (2, "hi"), (3, [(1, 7)]), (2, "again"))
        fields = parse_fields(blob)
        assert fields == [(1, 0, 150), (2, 2, b"hi"), (3, 2, pb((1, 7))), (2, 2, b"again")]
        assert first_field(fields, 2) == b"hi" and all_fields(fields, 2) == [b"hi", b"again"]
        assert field_int(fields, 1) == 150 and field_text(fields, 2) == "hi"
        assert field_int(sub_message(fields, 3), 1) == 7
        assert sub_message(fields, 9) is None and field_int(fields, 2) is None

    def test_fixed_width_values(self):
        blob = bytes([0x09]) + b"12345678" + bytes([0x15]) + b"abcd"
        assert parse_fields(blob) == [(1, 1, b"12345678"), (2, 5, b"abcd")]

    def test_large_varints(self):
        assert parse_fields(pb((4, unix_ms("2024-03-01T12:00:00")))) == [(4, 0, 1709294400000)]

    @pytest.mark.parametrize("blob", [
        b"\x08",                 # varint missing
        b"\x12\x05abc",          # length past the end
        b"\x00\x01",             # field number 0
        b"\x0b\x01",             # group start (wire type 3)
        b"\x0f",                 # wire type 7
        b"\x09abc",              # fixed64 cut short
        b"\x08" + b"\xff" * 11,  # varint never ends
    ])
    def test_malformed_input_raises(self, blob):
        with pytest.raises(ProtobufError):
            parse_fields(blob)

    def test_not_bytes_or_bad_nested_message(self):
        with pytest.raises(ProtobufError):
            parse_fields("text")
        assert sub_message(parse_fields(pb((1, b"\x12\x09short"))), 1) is None
        assert field_text(parse_fields(pb((1, b"\xff\xfe"))), 1) is None


# ── every message type, and the shapes the standard scenario doesn't have ───

VIDEO_BYTES, AUDIO_BYTES, PDF_BYTES, WEBP_BYTES = b"fake mp4" * 20, b"OggS fake opus" * 10, b"%PDF-1.4 fake", b"RIFF fake webp"


def types_backup(tmp_path, version):
    """One chat with a row of every message type the code knows, plus two groups."""
    R = jid(RILEY)
    files = {
        "Media/r/0/1/video.mp4": VIDEO_BYTES, "Media/r/0/1/voice.opus": AUDIO_BYTES, "Media/r/0/1/report.pdf": PDF_BYTES,
        "Media/r/0/1/sticker.webp": WEBP_BYTES, "Media/r/0/1/clip.mp4": VIDEO_BYTES, "Media/r/0/1/future.mp4": VIDEO_BYTES,
        "Media/r/0/1/captioned.jpg": IMAGE_BYTES,
    }

    def populate(db):
        chats, msgs, media, info = (db.table(n) for n in ("ZWACHATSESSION", "ZWAMESSAGE", "ZWAMEDIAITEM", "ZWAMESSAGEINFO"))
        data_items, groups = db.table("ZWAMESSAGEDATAITEM"), db.table("ZWAGROUPINFO")
        members = db.table("ZWAGROUPMEMBER")
        chats.insert({"Z_PK": 1, "Z_ENT": 4, "ZCONTACTJID": R, "ZSESSIONTYPE": 0, "ZPARTNERNAME": "Riley WA"})
        chats.insert({"Z_PK": 2, "Z_ENT": 4, "ZCONTACTJID": "120363000000000002@g.us", "ZSESSIONTYPE": 1, "ZPARTNERNAME": "Riley's group"})
        chats.insert({"Z_PK": 3, "Z_ENT": 4, "ZCONTACTJID": "120363000000000003@g.us", "ZSESSIONTYPE": 1, "ZPARTNERNAME": "Joined group"})
        groups.insert({"Z_PK": 1, "Z_ENT": 5, "ZCHATSESSION": 2, "ZCREATORJID": R})
        groups.insert({"Z_PK": 2, "Z_ENT": 5, "ZCHATSESSION": 3})  # creator unknown, as when someone else made it
        sort = iter(range(1, 1000))

        def row(pk, mtype, *, chat=1, from_me=False, event=None, text=None, media_pk=None, info_pk=None,
                member=None, to=None, sent=None, stanza=None, date=None):
            msgs.insert({"Z_PK": pk, "Z_ENT": 9, "ZCHATSESSION": chat, "ZSORT": next(sort), "ZMESSAGETYPE": mtype,
                         "ZGROUPEVENTTYPE": event if event is not None else (0 if from_me else 2),
                         "ZISFROMME": int(from_me), "ZMESSAGEDATE": date or T0 + pk, "ZTEXT": text,
                         "ZFROMJID": None if from_me else (R if chat == 1 else f"12036300000000000{chat}@g.us"),
                         "ZTOJID": to or (R if from_me else None), "ZMEDIAITEM": media_pk, "ZMESSAGEINFO": info_pk,
                         "ZGROUPMEMBER": member, "ZSENTDATE": sent, "ZSTANZAID": stanza or f"3A{pk:018d}"})

        def item(pk, msg_pk, **cols):
            media.insert({"Z_PK": pk, "Z_ENT": 8, "ZMESSAGE": msg_pk, **cols})

        row(1, 2, media_pk=1)
        item(1, 1, ZMEDIALOCALPATH="Media/r/0/1/video.mp4", ZVCARDSTRING="video/mp4", ZMOVIEDURATION=12.5, ZFILESIZE=len(VIDEO_BYTES))
        row(2, 3, media_pk=2)
        item(2, 2, ZMEDIALOCALPATH="Media/r/0/1/voice.opus", ZVCARDSTRING="audio/ogg; codecs=opus", ZMOVIEDURATION=7.0)
        row(3, 4, media_pk=3)
        item(3, 3, ZVCARDNAME="Alex Example_$!<Name-Separator>!$_Sam Example",
             ZVCARDSTRING="BEGIN:VCARD\nVERSION:3.0\nFN:Alex Example\nEND:VCARD")
        row(4, 4, media_pk=4)
        item(4, 4, ZVCARDSTRING="not a vcard")
        row(5, 7, text="see https://example.org/page")
        data_items.insert({"Z_PK": 1, "Z_ENT": 10, "ZMESSAGE": 5, "ZMATCHEDTEXT": "https://example.org/page",
                           "ZTITLE": "Example page", "ZSUMMARY": "A page about examples"})
        row(6, 8, media_pk=6)
        item(6, 6, ZMEDIALOCALPATH="Media/r/0/1/report.pdf", ZVCARDSTRING="application/pdf", ZTITLE="Quarterly report.pdf")
        row(7, 11, media_pk=7)
        item(7, 7, ZMEDIALOCALPATH="Media/r/0/1/clip.mp4", ZVCARDSTRING="video/mp4", ZMOVIEDURATION=3.0)
        row(8, 14)
        row(9, 14, from_me=True)
        row(10, 15, media_pk=10)
        item(10, 10, ZMEDIALOCALPATH="Media/r/0/1/sticker.webp", ZVCARDSTRING="image/webp")
        row(11, 38, media_pk=11)
        item(11, 11)
        row(12, 46, text="Lunch where?")
        row(13, 54, media_pk=13)
        item(13, 13)  # a video message that was never downloaded
        row(14, 77, media_pk=14)
        item(14, 14, ZMEDIALOCALPATH="Media/r/0/1/future.mp4", ZVCARDSTRING="video/mp4")
        row(15, 77)
        for pk, event in ((16, 1), (17, 4), (18, 45), (19, 46)):
            row(pk, 10, event=event)
        row(20, 59, media_pk=20)
        item(20, 20, ZMETADATA=call_meta(False, 0, 0))
        row(21, 59)  # a call row with no details
        row(22, 0, from_me=True, text="sent, with a server time", sent=T0 + 30)
        row(23, 0, text="two reactions", info_pk=1)
        info.insert({"Z_PK": 1, "Z_ENT": 11, "ZMESSAGE": 23, "ZRECEIPTINFO": pb((7, [
            (1, [(1, "B" * 32), (2, R), (3, "🙏"), (4, unix_ms("2024-03-01T13:00:00"))]),
            (2, [(1, "3AOWN2"), (2, "❤️"), (3, unix_ms("2024-03-01T12:30:00"))]),
        ]))})
        row(24, 0, from_me=True, text="something I said", stanza="3AMINE000000000000001")
        row(25, 0, text="quoting you", media_pk=25)
        item(25, 25, ZMETADATA=pb((5, "3AMINE000000000000001"), (19, [(1, "something I said")])))
        row(26, 1, media_pk=26, stanza="3ACAPTIONED0000000001")
        item(26, 26, ZMEDIALOCALPATH="Media/r/0/1/captioned.jpg", ZVCARDSTRING="image/jpeg", ZTITLE="Look at\nthis view")
        row(27, 0, from_me=True, text="nice", media_pk=27)
        item(27, 27, ZMETADATA=pb((5, "3ACAPTIONED0000000001"), (6, R), (19, [(3, [(2, "image/jpeg")])])))
        row(28, 0, from_me=True, text="about that old photo", media_pk=28)
        item(28, 28, ZMETADATA=pb((5, "3AGONEPHOTO0000000001"), (6, R), (19, [(3, [(3, "Old caption")])])))
        row(29, 0, from_me=True, text="about that file", media_pk=29)
        item(29, 29, ZMETADATA=pb((5, "3AGONEFILE00000000001"), (19, [(7, [(8, "notes.txt")])])))
        # Groups: created by Riley; and one someone else made that the owner was added to.
        row(40, 10, chat=2, event=2, to=jid(OWNER))
        row(41, 6, chat=2, event=12, to=jid(OWNER))
        row(50, 10, chat=3, event=2, to=jid(OWNER))
        row(51, 6, chat=3, event=12, to=jid(OWNER))
        row(52, 6, chat=3, event=2, to=jid(OWNER))  # the real shape: no member, no text
        row(53, 6, chat=3, event=2, to=jid(OWNER), member=99)  # a member that isn't there
        members.insert({"Z_PK": 1, "Z_ENT": 6, "ZCHATSESSION": 3, "ZMEMBERJID": jid(OWNER)})

    build(tmp_path / "ChatStorage.sqlite", version, "ChatStorage.sqlite", populate)
    entries = {(SHARED_DOMAIN, "ChatStorage.sqlite"): str(tmp_path / "ChatStorage.sqlite")}
    prefs = tmp_path / "prefs.plist"
    prefs.write_bytes(plistlib.dumps({"OwnJabberID": jid(OWNER)}, fmt=plistlib.FMT_BINARY))
    entries[(SHARED_DOMAIN, PLIST)] = str(prefs)
    for i, (rel, data) in enumerate(files.items()):
        local = tmp_path / f"media{i}"
        local.write_bytes(data)
        entries[(SHARED_DOMAIN, "Message/" + rel)] = str(local)
    return FakeBackup(entries)


@pytest.fixture(params=VERSIONS, ids=lambda v: f"ios-{v}")
def kinds(request, tmp_path):
    backup = types_backup(tmp_path, request.param)
    wa = WhatsAppExtractor()
    return backup, {c: by_id(wa.get_messages(backup, c, CONTACTS, limit=1000)["messages"]) for c in (1, 2, 3)}


class TestMessageTypes:
    def test_media_types(self, kinds):
        _, chats = kinds
        m = chats[1]

        def att(pk):
            return m[pk]["attachments"][0]
        assert (m[1]["message_type"], att(1)["mime_type"], att(1)["duration"], att(1)["available"]) == ("video", "video/mp4", 12.5, True)
        assert (att(1)["width"], att(1)["height"]) == (None, None)  # only images store dimensions there
        assert (m[2]["message_type"], att(2)["mime_type"], att(2)["duration"]) == ("audio", "audio/ogg; codecs=opus", 7.0)
        assert (m[6]["message_type"], m[6]["text"], att(6)["transfer_name"], att(6)["mime_type"]) == (
            "document", "", "Quarterly report.pdf", "application/pdf")
        assert (m[7]["message_type"], m[10]["message_type"], att(10)["mime_type"]) == ("gif", "sticker", "image/webp")
        assert (m[13]["message_type"], att(13)["available"], att(13)["filename"], att(13)["transfer_name"]) == (
            "video", False, "", "Video message")
        assert (m[14]["message_type"], att(14)["available"]) == ("video", True)  # an unknown type, known by its MIME
        assert (m[15]["message_type"], m[15]["event_text"]) == ("unknown", "Unsupported message")

    def test_contact_cards_links_polls_view_once_and_deletions(self, kinds):
        m = kinds[1][1]
        assert (m[3]["message_type"], m[3]["text"]) == ("contact", "Alex Example, Sam Example")
        assert m[3]["vcard"].startswith("BEGIN:VCARD")
        assert (m[4]["text"], m[4]["vcard"], m[4]["event_text"]) == ("", None, "Contact card")
        assert m[5]["message_type"] == "link"
        assert m[5]["link_preview"] == {"url": "https://example.org/page", "title": "Example page", "summary": "A page about examples"}
        assert (m[12]["message_type"], m[12]["text"], m[12]["event_text"]) == ("poll", "Lunch where?", "Poll")
        assert (m[11]["message_type"], m[11]["event_text"], m[11]["attachments"][0]["transfer_name"]) == (
            "view_once", "View-once photo", "View-once photo")
        assert (m[8]["event_text"], m[9]["event_text"]) == ("This message was deleted", "You deleted this message")
        assert all(m[pk]["vcard"] is None for pk in m if pk not in (3,))

    def test_missed_call_notices_and_calls_without_details(self, kinds):
        m = kinds[1][1]
        assert [(m[pk]["message_type"], m[pk]["event_text"]) for pk in (16, 17, 18, 19)] == [
            ("call", "Missed voice call"), ("call", "Missed video call"), ("call", "Missed voice call"), ("call", "Missed video call")]
        assert m[20]["call"]["outcome"] == "missed" and m[20]["event_text"] == "Missed voice call"
        assert m[21]["call"] == {"is_video": None, "duration": None, "outcome": "unknown", "direction": "incoming"}
        assert m[21]["event_text"] == "WhatsApp call"

    def test_received_date_is_only_for_received_messages(self, kinds):
        assert kinds[1][1][22]["received_date"] is None

    def test_reactions_in_time_order(self, kinds):
        reactions = kinds[1][1][23]["reactions"]
        assert [(r["emoji"], r["is_from_me"]) for r in reactions] == [("❤️", True), ("🙏", False)]

    def test_quotes_of_my_own_message_and_of_captioned_photos(self, kinds):
        m = kinds[1][1]
        assert m[25]["quoted"] == {"message_id": 24, "text": "something I said", "message_type": "text",
                                   "is_from_me": True, "sender": "You"}
        assert m[26]["text"] == "Look at\nthis view"  # captions keep their line breaks
        assert m[27]["quoted"]["text"] == "Look at\nthis view" and m[27]["quoted"]["message_id"] == 26
        assert (m[28]["quoted"]["text"], m[28]["quoted"]["message_type"]) == ("Old caption", "image")
        assert (m[29]["quoted"]["text"], m[29]["quoted"]["message_type"], m[29]["quoted"]["sender"]) == (
            "notes.txt", "document", "You")

    def test_group_creation_and_joining(self, kinds):
        created_by_riley, joined = kinds[1][2], kinds[1][3]
        assert created_by_riley[41]["event_text"] == "Riley Sample created the group"
        assert joined[51]["event_text"] == "The group was created"
        assert joined[52]["event_text"] == "You were added"
        assert joined[53]["event_text"] == "A member was added"  # a dangling member is never "You"

    def test_search_order_and_limit(self, kinds):
        backup, _ = kinds
        wa = WhatsAppExtractor()
        hits = wa.search_messages(backup, "e", CONTACTS)["results"]
        assert len(hits) > 3
        assert [h["date"] for h in hits] == sorted(h["date"] for h in hits)
        assert wa.search_messages(backup, "e", CONTACTS, limit=2)["results"] == hits[:2]
        assert wa.search_messages(backup, "e", CONTACTS, limit=0)["results"] == []
        assert wa.search_messages(backup, "e", CONTACTS, limit=None)["results"] == hits
        assert [h["message_id"] for h in wa.search_messages(backup, "quarterly", CONTACTS)["results"]] == [6]


# ── names ────────────────────────────────────────────────────────────────────

class TestNames:
    def test_lid_digits_never_reach_the_address_book(self, latest):
        decoy = {**CONTACTS, STRANGER_LID[-10:]: "Wrong Person"}
        convs = {c["chat_id"]: c for c in WhatsAppExtractor().list_conversations(latest.backup, decoy)["conversations"]}
        assert convs[7]["display_name"] == "Unknown contact"
        (msg,) = WhatsAppExtractor().get_messages(latest.backup, 7, decoy)["messages"]
        assert msg["sender"] == "Unknown member"

    def test_address_book_beats_whatsapps_own_names(self, store):
        contacts = {**CONTACTS, "2025550102": "Casey From The Address Book"}
        m = by_id(WhatsAppExtractor().get_messages(store.backup, 2, contacts, limit=100)["messages"])
        assert m[33]["sender"] == m[38]["sender"] == "Casey From The Address Book"

    def test_without_an_address_book_the_chat_name_is_whatsapps(self, store):
        convs = {c["chat_id"]: c for c in WhatsAppExtractor().list_conversations(store.backup, {})["conversations"]}
        assert convs[1]["display_name"] == "Riley WA"

    def test_group_member_name_beats_push_name(self, tmp_path):
        version = next(v for v in reversed(VERSIONS) if not has_schema(v, "ContactsV2.sqlite"))
        store = Store(tmp_path, version)
        conn = sqlite3.connect(store.files[(SHARED_DOMAIN, "ChatStorage.sqlite")])
        conn.execute("UPDATE ZWAGROUPMEMBER SET ZCONTACTNAME = 'Casey In The Group' WHERE Z_PK = 1")
        conn.commit()
        conn.close()
        m = by_id(all_messages(store, 2))
        assert m[33]["sender"] == "Casey In The Group"

    def test_lid_display_names(self, tmp_path):
        version = next(v for v in VERSIONS if has_schema(v, "LID.sqlite"))
        store = Store(tmp_path, version)
        conn = sqlite3.connect(store.files[(SHARED_DOMAIN, "LID.sqlite")])
        conn.execute("INSERT INTO ZWALIDPROPERTIES (Z_PK, Z_ENT, ZLID, ZDISPLAYNAME) VALUES (1, 1, ?, 'Sam Stranger')",
                     (STRANGER_LID,))
        conn.commit()
        conn.close()
        convs = {c["chat_id"]: c for c in WhatsAppExtractor().list_conversations(store.backup, CONTACTS)["conversations"]}
        assert convs[7]["display_name"] == "Sam Stranger"
        assert all_messages(store, 7)[0]["sender"] == "Sam Stranger"


# ── robustness ───────────────────────────────────────────────────────────────

def _update(store, *statements, db="ChatStorage.sqlite"):
    conn = sqlite3.connect(store.files[(SHARED_DOMAIN, db)])
    for sql in statements:
        conn.execute(sql)
    conn.commit()
    conn.close()


class TestRobustness:
    def test_cache_follows_the_store_and_the_accessor(self, latest):
        wa = WhatsAppExtractor()
        assert wa.get_messages(latest.backup, 1, CONTACTS, limit=100)["messages"][3]["attachments"][0]["available"]
        _update(latest, "UPDATE ZWAMEDIAITEM SET ZMEDIALOCALPATH = 'Media/x/new.jpg' WHERE Z_PK = 2",
                "UPDATE ZWAGROUPMEMBER SET ZCONTACTNAME = 'Renamed Casey' WHERE Z_PK = 1")
        latest._add("Message/Media/x/new.jpg", IMAGE_BYTES)
        fresh = FakeBackup(latest.files)  # a reopened backup is a new accessor
        m = by_id(wa.get_messages(fresh, 1, CONTACTS, limit=100)["messages"])
        assert m[13]["attachments"][0]["available"] is True
        assert m[13]["attachments"][0]["filename"] == "Message/Media/x/new.jpg"
        wa.clear_cache()
        assert by_id(wa.get_messages(fresh, 1, CONTACTS, limit=100)["messages"])[13]["attachments"][0]["available"]

    def test_damaged_text_is_replaced_not_fatal(self, latest):
        _update(latest, "UPDATE ZWAMESSAGE SET ZTEXT = CAST(X'48C3FF21' AS TEXT) WHERE Z_PK = 12",
                "UPDATE ZWAPROFILEPUSHNAME SET ZPUSHNAME = CAST(X'C328' AS TEXT)")
        m = by_id(all_messages(latest, 1))
        assert m[12]["text"].startswith("H") and "�" in m[12]["text"]
        assert WhatsAppExtractor().search_messages(latest.backup, "Reply", CONTACTS)["results"]

    def test_odd_values_in_numeric_columns(self, latest):
        _update(latest, "UPDATE ZWAMESSAGE SET ZMESSAGETYPE = 'abc' WHERE Z_PK = 19",
                "UPDATE ZWAMEDIAITEM SET ZFILESIZE = 'big', ZLATITUDE = 9e999, ZLONGITUDE = -9e999 WHERE Z_PK = 2",
                "UPDATE ZWAMEDIAITEM SET ZLATITUDE = 9e999 WHERE Z_PK = 6",
                "UPDATE ZWACHATSESSION SET ZCONTACTJID = 42 WHERE Z_PK = 5")
        m = by_id(all_messages(latest, 1))
        assert m[19]["message_type"] == "unknown"
        att = m[13]["attachments"][0]
        assert (att["total_bytes"], att["width"], att["height"]) == (None, None, None)
        assert m[17]["location"]["latitude"] is None
        import json
        json.dumps(m, allow_nan=False)
        assert "error" not in WhatsAppExtractor().list_conversations(latest.backup, CONTACTS)

    def test_parameters_are_coerced(self, latest):
        wa = WhatsAppExtractor()
        assert wa.get_messages(latest.backup, "1", CONTACTS, limit=3)["total"] == 14
        page = wa.get_messages(latest.backup, 1, CONTACTS, offset="x", limit=10 ** 30)
        assert (page["offset"], len(page["messages"])) == (0, 14)
        assert wa.get_messages(latest.backup, None, CONTACTS)["messages"] == []
        assert wa.get_messages(latest.backup, 1, CONTACTS, offset=-5, limit=-1)["messages"] == []
        assert wa.get_message_offset(latest.backup, "1", "12")["found"] is True
        assert wa.search_messages(latest.backup, "Riley", CONTACTS, chat_id="1")["results"]
        assert wa.get_attachment(latest.backup, "2")["mime_type"] == "image/jpeg"

    def test_offset_of_a_message_without_sort(self, latest):
        _update(latest, "UPDATE ZWAMESSAGE SET ZSORT = NULL WHERE Z_PK = 19")
        wa = WhatsAppExtractor()
        ids = [m["message_id"] for m in all_messages(latest, 1)]
        assert ids[0] == 19  # NULL sorts oldest
        assert wa.get_message_offset(latest.backup, 1, 19) == {"offset": len(ids) - 1, "found": True}
        for index, pk in enumerate(ids):
            assert wa.get_message_offset(latest.backup, 1, pk)["offset"] == len(ids) - 1 - index

    def test_paging_ties_without_sort_column(self, tmp_path):
        path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID VARCHAR);"
            "CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMESSAGETYPE INTEGER, "
            "ZMESSAGEDATE TIMESTAMP, ZTEXT VARCHAR);"
            "INSERT INTO ZWACHATSESSION VALUES (1, '12025550101@s.whatsapp.net');"
        )
        for pk, date in ((1, 10.0), (2, 20.0), (3, 20.0), (4, 30.0)):
            conn.execute("INSERT INTO ZWAMESSAGE VALUES (?, 1, 0, ?, 'x')", (pk, 700000000 + date))
        conn.commit()
        conn.close()
        backup = FakeBackup({(SHARED_DOMAIN, "ChatStorage.sqlite"): str(path)})
        wa = WhatsAppExtractor()
        pages = [wa.get_messages(backup, 1, {}, offset=o, limit=2)["messages"] for o in (0, 2)]
        ids = [m["message_id"] for page in reversed(pages) for m in page]
        assert ids == [1, 2, 3, 4]
        assert [wa.get_message_offset(backup, 1, pk)["offset"] for pk in ids] == [3, 2, 1, 0]

    def test_an_accessor_that_raises(self, latest):
        class Broken(FakeBackup):
            def list_files(self, domain=None, path_like=None):
                raise OSError("manifest gone")

            def get_file(self, relative_path, domain="HomeDomain"):
                if relative_path == PLIST:
                    raise OSError("cannot decrypt")
                return super().get_file(relative_path, domain)
        backup = Broken(latest.files)
        wa = WhatsAppExtractor()
        convs = wa.list_conversations(backup, CONTACTS)["conversations"]
        assert convs and convs[0]["chat_id"] == 1
        m = by_id(wa.get_messages(backup, 1, CONTACTS, limit=100)["messages"])
        assert m[13]["attachments"][0]["available"] is False  # no listing: nothing can be confirmed
        assert m[11]["sender_handle"] == "+12025550100"  # the owner still comes from the system rows
        assert wa.list_conversations(Broken(), CONTACTS) == {"conversations": [], "empty_reason": "not_in_backup"}

    def test_zero_length_store_is_unreadable(self, tmp_path):
        empty = tmp_path / "ChatStorage.sqlite"
        empty.write_bytes(b"")
        backup = FakeBackup({(SHARED_DOMAIN, "ChatStorage.sqlite"): str(empty)})
        result = WhatsAppExtractor().list_conversations(backup, CONTACTS)
        assert result["conversations"] == [] and "empty_reason" not in result and "could not be read" in result["error"]

    def test_every_method_reports_the_same_failure(self, tmp_path):
        path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE ZSOMETHINGELSE (Z_PK INTEGER PRIMARY KEY)")
        conn.close()
        backup = FakeBackup({(SHARED_DOMAIN, "ChatStorage.sqlite"): str(path)})
        wa = WhatsAppExtractor()
        listed = wa.list_conversations(backup, CONTACTS)
        for result in (wa.has_whatsapp(backup), wa.search_messages(backup, "x", CONTACTS),
                       wa.get_messages(backup, 1, CONTACTS)):
            assert (result["error"], result["empty_reason"]) == (listed["error"], "unsupported_format")


# ── a real backup, left exactly as it was ────────────────────────────────────

def test_a_read_only_unencrypted_backup_is_left_unchanged(tmp_path):
    (tmp_path / "parts").mkdir()
    store = Store(tmp_path / "parts", VERSIONS[-1])
    device = Device(name="Test iPhone", product_type="iPhone12,1", product_name="iPhone 11",
                    ios="17.3", build="21D50", phone_number="+1 202 555 0100")
    writer = BackupWriter(tmp_path / "out", device, datetime(2024, 3, 2, tzinfo=timezone.utc), "17.3", seed=1)
    for (domain, rel), local in store.files.items():
        writer.add_file(domain, rel, Path(local).read_bytes())
    backup_dir = writer.finalize()
    before = _snapshot(backup_dir)
    with _read_only(backup_dir):
        accessor = open_local_backup(str(backup_dir))
        wa = WhatsAppExtractor()
        assert [c["chat_id"] for c in wa.list_conversations(accessor, CONTACTS)["conversations"]] == [1, 2, 5, 7]
        for chat_id in (1, 2, 5, 7):
            assert "error" not in wa.get_messages(accessor, chat_id, CONTACTS, limit=100)
        assert wa.search_messages(accessor, "e", CONTACTS)["results"]
        assert Path(wa.get_attachment(accessor, 2)["path"]).read_bytes() == IMAGE_BYTES
        assert wa.has_whatsapp(accessor)["has_whatsapp"]
    assert _snapshot(backup_dir) == before
