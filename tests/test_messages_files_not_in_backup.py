"""A photo or file whose attachment record is gone is an attachment not in the backup.

When iOS removes a message's attachment (its sms.db record and its file) but
keeps the message, the row has no text, no message_attachment_join row and
cache_has_attachments 0. Its attributedBody is all that's left: a U+FFFC
placeholder per file, each carrying __kIMFileTransferGUIDAttributeName. Those
rows used to come back as "system" (the app's "Conversation update"). They are
now "attachment", each file marked the way a file the backup lacks is:
``available`` False, with no id, name or type, since sms.db has no record of it.

The bodies are built by hand in Apple's typedstream layout (backupgen's, with
the file-transfer attribute added to each run); each sms.db from the real table
definitions harvested in backupgen/schemas.
"""

import json
import os
import plistlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen.typedstream import _HEAD, _integer, attributed_body  # noqa: E402
from ios_backup_core.extractors.messages import MessageExtractor  # noqa: E402
from ios_backup_core.text import file_placeholders, parse_attributed_body  # noqa: E402

SCHEMAS = Path(ROOT).resolve() / "backupgen" / "schemas"
SCHEMA_VERSIONS = sorted(p.parent.name for p in SCHEMAS.glob("ios-*/sms.db.json"))
TABLES = ("message", "handle", "chat_handle_join", "chat_message_join",
          "attachment", "message_attachment_join")
KARIN = 1
CONTACTS = {"+15550001111": "Karin"}

FILE_TRANSFER_KEY = b"__kIMFileTransferGUIDAttributeName"
MESSAGE_PART_KEY = b"__kIMMessagePartAttributeName"


def _string(value: bytes) -> bytes:
    """An NSString object in a run's attributes (its class and "+" already in the stream)."""
    return b"\x92\x84\x96\x96" + _integer(len(value)) + value + b"\x86"


def file_body(guids: list, text: str = "") -> bytes:
    """attributedBody for ``len(guids)`` files, then ``text``: one run per file, as Messages writes it.

    Each U+FFFC run carries the file's guid and its message part. ``text``,
    if given, follows in one more run with only a message part.
    """
    body = "￼" * len(guids) + text
    data = body.encode("utf-8")
    out = _HEAD + _integer(len(data)) + data + b"\x86"
    runs = [(guid, 1) for guid in guids]
    if text:
        runs.append((None, len(text.encode("utf-16-le")) // 2))
    for index, (guid, length) in enumerate(runs):
        first = index == 0
        out += (b"\x84\x02iI" if first else b"\x97") + _integer(index + 1) + _integer(length)
        entries = 2 if guid else 1
        # NSDictionary: new class the first time, then by reference
        out += (b"\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i" if first else b"\x92\x84\x98\x99")
        out += _integer(entries)
        if guid:
            out += _string(FILE_TRANSFER_KEY) + _string(guid.encode("ascii"))
        out += _string(MESSAGE_PART_KEY)
        # NSNumber holding the part's index: new class the first time, then by reference
        out += (b"\x92\x84\x84\x84\x08NSNumber\x00\x84\x84\x07NSValue\x00\x94\x84\x01*\x84\x99\x99" if first
                else b"\x92\x84\x9d\x9c\x84\x99\x99")
        out += _integer(index) + b"\x86\x86"
    return out + b"\x86"


ONE_PHOTO = file_body(["at_0_6F1D2C1E-0000-4000-8000-000000000001"])
TWO_FILES = file_body(["at_0_6F1D2C1E-0000-4000-8000-000000000002",
                       "at_1_6F1D2C1E-0000-4000-8000-000000000003"])


def _summary(**info) -> bytes:
    return plistlib.dumps(info, fmt=plistlib.FMT_BINARY)


class _FakeBackup:
    def __init__(self, sms_path: str):
        self._sms_path = sms_path

    def get_file(self, relative_path: str, domain: str = None):
        return self._sms_path if relative_path == "Library/SMS/sms.db" else None


# message_id -> columns
ROWS = {
    # Photo whose record is gone (RCS, SMS and iMessage alike)
    1: dict(attributedBody=ONE_PHOTO, service="RCS"),
    2: dict(attributedBody=TWO_FILES, is_from_me=1, handle_id=0, service="SMS"),
    # cache_has_attachments still set, but no record: the same
    3: dict(attributedBody=ONE_PHOTO, cache_has_attachments=1, service="iMessage"),
    # A file whose record is there
    4: dict(attributedBody=file_body(["at_0_PRESENT"]), cache_has_attachments=1),
    # An ordinary message
    5: dict(text="See you at 6", attributedBody=attributed_body("See you at 6")),
    # A placeholder that isn't a file, and no body at all: still unknown
    6: dict(attributedBody=attributed_body("￼")),
    7: dict(),
    # Unsent stays unsent
    8: dict(attributedBody=ONE_PHOTO,
            message_summary_info=_summary(ust=True, rp=[0], otr={"0": {"lo": 0, "le": 1}})),
    # A group change stays a group change
    9: dict(item_type=2, group_title="Trip", attributedBody=ONE_PHOTO),
}


def _build_sms_db(path: str, version: str) -> None:
    schema = json.loads((SCHEMAS / version / "sms.db.json").read_text(encoding="utf-8"))
    conn = sqlite3.connect(path)
    for obj in schema["objects"]:
        if obj["type"] == "table" and obj["name"] in TABLES:
            conn.execute(obj["sql"])
    columns = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
    conn.execute("INSERT INTO handle (ROWID, id, service) VALUES (?, '+15550001111', 'iMessage')", (KARIN,))
    conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, ?)", (KARIN,))
    for message_id, values in ROWS.items():
        row = {"ROWID": message_id, "guid": f"guid-{message_id}", "date": message_id * 1_000_000_000,
               "is_from_me": 0, "handle_id": KARIN, "text": None, "cache_has_attachments": 0, **values}
        row = {k: v for k, v in row.items() if k in columns}
        conn.execute(f"INSERT INTO message ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, ?)", (message_id,))
    attachment = {"ROWID": 40, "guid": "at_0_PRESENT", "original_guid": "at_0_PRESENT",
                  "filename": "~/Library/SMS/Attachments/ab/11/at_0_PRESENT/IMG_0040.HEIC",
                  "mime_type": "image/heic", "transfer_name": "IMG_0040.HEIC", "total_bytes": 1234}
    attachment_columns = {r[1] for r in conn.execute("PRAGMA table_info(attachment)")}
    attachment = {k: v for k, v in attachment.items() if k in attachment_columns}
    conn.execute(f"INSERT INTO attachment ({', '.join(attachment)}) VALUES ({', '.join('?' * len(attachment))})",
                 list(attachment.values()))
    conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (4, 40)")
    conn.commit()
    conn.close()


@pytest.fixture(params=SCHEMA_VERSIONS)
def messages(request, tmp_path):
    sms_path = str(tmp_path / "sms.db")
    _build_sms_db(sms_path, request.param)
    extractor = MessageExtractor()
    try:
        result = extractor.get_messages(_FakeBackup(sms_path), chat_id=1, contacts=CONTACTS, limit=100)
    finally:
        extractor.close()
    assert "error" not in result
    with sqlite3.connect(sms_path) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
    return {m["message_id"]: m for m in result["messages"]}, columns


NOT_IN_BACKUP = {"attachment_id": None, "filename": "", "mime_type": None, "transfer_name": "",
                 "total_bytes": None, "available": False}


# ── The bodies ────────────────────────────────────────────────────────────────

def test_the_bodies_are_whole_typedstreams():
    """Each hand-built body unarchives as Messages' own does: the placeholders, one run per file."""
    import typedstream
    for body, files in ((ONE_PHOTO, 1), (TWO_FILES, 2), (file_body(["at_0_X"], text="Look"), 1)):
        archived = typedstream.unarchive_from_data(body)
        dictionaries = [c.value for c in archived.contents if getattr(c, "encoding", None) == b"@"][1:]
        guids = [d.contents for d in dictionaries if any(k.value == FILE_TRANSFER_KEY.decode() for k in d.contents)]
        assert len(guids) == files


def test_parse_attributed_body_finds_no_text_in_them():
    assert parse_attributed_body(ONE_PHOTO) == ("", "text")
    assert parse_attributed_body(TWO_FILES) == ("", "text")


@pytest.mark.parametrize("body, files", [
    (ONE_PHOTO, 1),
    (TWO_FILES, 2),
    (file_body(["at_0_X"], text="Look at this"), 0),   # a caption is text, not a missing file
    (attributed_body("￼"), 0),                    # a placeholder that isn't a file
    (attributed_body("See you at 6"), 0),
    (b"", 0),
    (None, 0),
    (b"bplist00" + FILE_TRANSFER_KEY, 0),
    (b"\x04\x0bstreamtyped" + FILE_TRANSFER_KEY, 0),   # unreadable
])
def test_file_placeholders(body, files):
    assert file_placeholders(body) == files


# ── get_messages ──────────────────────────────────────────────────────────────

def test_schemas_were_found():
    assert "ios-13.3.1" in SCHEMA_VERSIONS and "ios-26.5" in SCHEMA_VERSIONS


@pytest.mark.parametrize("message_id, files", [(1, 1), (2, 2), (3, 1)])
def test_a_file_whose_record_is_gone_is_an_attachment_not_in_the_backup(messages, message_id, files):
    msg = messages[0][message_id]
    assert msg["message_type"] == "attachment"
    assert msg["has_attachments"] is True
    assert msg["attachments"] == [NOT_IN_BACKUP] * files
    assert msg["text"] == "" and msg["event_text"] is None


def test_a_file_with_its_record_is_unchanged(messages):
    msg = messages[0][4]
    assert msg["message_type"] == "attachment"
    assert [a["attachment_id"] for a in msg["attachments"]] == [40]
    assert msg["attachments"][0]["transfer_name"] == "IMG_0040.HEIC"
    assert "available" not in msg["attachments"][0]


def test_an_ordinary_message_is_unchanged(messages):
    msg = messages[0][5]
    assert (msg["message_type"], msg["text"], msg["has_attachments"], msg["attachments"]) == (
        "text", "See you at 6", False, [])


@pytest.mark.parametrize("message_id", [6, 7])
def test_rows_that_really_are_unknown_stay_system(messages, message_id):
    msg = messages[0][message_id]
    assert (msg["message_type"], msg["has_attachments"], msg["attachments"]) == ("system", False, [])


def test_unsent_stays_unsent(messages):
    by_id, columns = messages
    if "message_summary_info" not in columns:
        pytest.skip("this sms.db has no message_summary_info, which says a message was unsent")
    assert (by_id[8]["message_type"], by_id[8]["attachments"]) == ("unsent", [])


def test_a_group_change_stays_a_group_change(messages):
    by_id, _ = messages
    assert (by_id[9]["message_type"], by_id[9]["attachments"]) == ("group_renamed", [])
