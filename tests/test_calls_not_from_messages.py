"""list_calls reads calls from CallHistory (and voicemails), never from sms.db.

sms.db's item_type 1 and 2 rows used to be read as FaceTime video and audio
calls. They are group changes: item_type 1 is someone added to or removed from
a group (group_action_type 0 or 1), item_type 2 a group renamed. On a real
iOS 27 backup that added dozens of answered, zero-length "calls" from groups,
many of them long deleted. item_type 6, a FaceTime call or SharePlay in a
conversation, isn't read either: CallHistory has the call itself, with its
length and whether it was answered.

Each sms.db is built from the real table definitions harvested in
backupgen/schemas, so these rows sit in the columns iOS really ships.
"""

import json
import os
import plistlib
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.calls import CallExtractor  # noqa: E402
from ios_backup_core.extractors.messages import MessageExtractor  # noqa: E402

SCHEMAS = Path(__file__).resolve().parent.parent / "backupgen" / "schemas"
SCHEMA_VERSIONS = sorted(p.parent.name for p in SCHEMAS.glob("ios-*/sms.db.json"))
SMS_DB = "Library/SMS/sms.db"
CALL_DB = "Library/CallHistoryDB/CallHistory.storedata"
TABLES = ("message", "handle", "chat_handle_join", "chat_message_join",
          "attachment", "message_attachment_join")

KARIN, THEO = 1, 2
NUMBERS = {KARIN: "+15550001111", THEO: "+15550002222"}
CONTACTS = {"+15550001111": "Karin", "+15550002222": "Theo"}

# Apple seconds (since 2001) for each sms.db row, an hour apart, well away
# from the calls below.
HOUR = 3600
SMS_BASE = 700_000_000


def _summary(**info) -> bytes:
    return plistlib.dumps(info, fmt=plistlib.FMT_BINARY)


# message_id -> columns. What Messages writes for group changes and FaceTime.
SMS_ROWS = {
    1: dict(item_type=1, group_action_type=0, handle_id=KARIN, other_handle=THEO),   # Karin added Theo
    2: dict(item_type=1, group_action_type=1, is_from_me=1, handle_id=0, other_handle=THEO),  # you removed Theo
    3: dict(item_type=2, handle_id=KARIN, group_title="Trip"),                        # Karin renamed it
    4: dict(item_type=2, is_from_me=1, handle_id=0, group_title=None),                # you removed the name
    5: dict(item_type=1, group_action_type=0, handle_id=THEO, other_handle=KARIN,
            message_summary_info=_summary(uat=True)),                                # unattributed add
    6: dict(item_type=6, handle_id=KARIN, message_summary_info=_summary(conversationAVMode=2)),  # FaceTime
    7: dict(item_type=6, handle_id=THEO, message_summary_info=_summary(conversationAVMode=0)),   # SharePlay
    8: dict(text="See you at 6", handle_id=KARIN),
}


class _FakeBackup:
    """Serves local files by relative path; lists them the way a manifest does."""

    def __init__(self, files, encrypted):
        self.files = dict(files)
        self.encrypted = encrypted

    def get_file(self, relative_path, domain="HomeDomain"):
        return self.files.get(relative_path)

    def list_files(self, domain=None, path_like=None):
        paths = list(self.files)
        if path_like:
            pattern = ".*".join(re.escape(p) for p in path_like.split("%"))
            paths = [p for p in paths if re.fullmatch(pattern, p, re.IGNORECASE)]
        return [{"hash": p, "domain": "HomeDomain", "path": p} for p in paths]


def _sms_db(path: Path, version: str) -> str:
    schema = json.loads((SCHEMAS / version / "sms.db.json").read_text(encoding="utf-8"))
    conn = sqlite3.connect(path)
    for obj in schema["objects"]:
        if obj["type"] == "table" and obj["name"] in TABLES:
            conn.execute(obj["sql"])
    columns = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
    for rowid, number in NUMBERS.items():
        conn.execute("INSERT INTO handle (ROWID, id, service) VALUES (?, ?, 'iMessage')", (rowid, number))
        conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, ?)", (rowid,))
    for message_id, values in SMS_ROWS.items():
        # Nanoseconds since 2001, as iOS 11 and later store them
        row = {"ROWID": message_id, "guid": f"guid-{message_id}", "is_from_me": 0, "text": None,
               "date": (SMS_BASE + message_id * HOUR) * 1_000_000_000, **values}
        row = {k: v for k, v in row.items() if k in columns}
        conn.execute(f"INSERT INTO message ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, ?)", (message_id,))
    conn.commit()
    conn.close()
    return str(path)


def _call_db(path: Path) -> str:
    """A FaceTime video call with Karin (at the time of sms.db's FaceTime row) and a phone call with Theo."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE ZCALLRECORD (
            Z_PK INTEGER PRIMARY KEY, ZADDRESS TEXT, ZNAME TEXT, ZDATE REAL, ZDURATION REAL,
            ZCALLTYPE INTEGER, ZORIGINATED INTEGER, ZANSWERED INTEGER, ZSERVICE_PROVIDER TEXT
        )
    """)
    conn.executemany("INSERT INTO ZCALLRECORD VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)", [
        (1, NUMBERS[KARIN], SMS_BASE + 6 * HOUR, 300.0, 8, 0, 1, "com.apple.FaceTime"),
        (2, NUMBERS[THEO], SMS_BASE + 20 * HOUR, 42.0, 1, 1, 0, "com.apple.Telephony"),
    ])
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture(params=SCHEMA_VERSIONS)
def sms_db(request, tmp_path):
    return _sms_db(tmp_path / "sms.db", request.param)


def _list_calls(files, encrypted):
    return CallExtractor().list_calls(_FakeBackup(files, encrypted), CONTACTS)


def test_schemas_were_found():
    assert "ios-13.3.1" in SCHEMA_VERSIONS and "ios-26.5" in SCHEMA_VERSIONS


def test_the_rows_are_group_changes_and_facetime_in_messages(sms_db):
    """The same rows, as the conversation shows them: group changes and FaceTime, not calls."""
    extractor = MessageExtractor()
    try:
        page = extractor.get_messages(_FakeBackup({SMS_DB: sms_db}, True), chat_id=1, contacts=CONTACTS)
    finally:
        extractor.close()
    types = {m["message_id"]: m["message_type"] for m in page["messages"]}
    assert types[3] == "group_renamed" and types[4] == "group_name_removed"
    assert types[6] == types[7] == "facetime" and types[8] == "text"
    # Schemas with group_action_type and other_handle name who was added or removed
    assert types[1] in ("group_member_added", "system") and types[2] in ("group_member_removed", "system")


def test_group_changes_and_facetime_rows_add_no_calls(sms_db, tmp_path):
    result = _list_calls({SMS_DB: sms_db, CALL_DB: _call_db(tmp_path / "calls.db")}, encrypted=True)
    assert [c["call_id"] for c in result["calls"]] == [2, 1]
    assert result["total"] == 2
    assert not any(str(c["call_id"]).startswith("ft_") for c in result["calls"])


def test_facetime_calls_come_from_call_history(sms_db, tmp_path):
    result = _list_calls({SMS_DB: sms_db, CALL_DB: _call_db(tmp_path / "calls.db")}, encrypted=True)
    facetime = next(c for c in result["calls"] if c["call_id"] == 1)
    assert facetime["app"] == "FaceTime Video"
    assert (facetime["duration"], facetime["status"], facetime["direction"]) == (300.0, "answered", "incoming")
    assert facetime["contact_name"] == "Karin"


def test_unencrypted_backup_still_advises_encryption(sms_db):
    """An unencrypted backup has no CallHistory. sms.db's rows used to fill the
    Calls list with group changes and hide that call history needs encryption."""
    result = _list_calls({SMS_DB: sms_db}, encrypted=False)
    assert result["calls"] == [] and result["total"] == 0
    assert result["empty_reason"] == "requires_encryption"


def test_encrypted_backup_without_call_history_is_empty(sms_db):
    result = _list_calls({SMS_DB: sms_db}, encrypted=True)
    assert result["calls"] == []
    assert result["empty_reason"] == "not_in_backup"
