"""Tests for how MessageExtractor.get_messages names conversation events:
group changes, unsent messages and FaceTime calls.

Each sms.db is built from the real table definitions harvested in
backupgen/schemas (iOS 13.3.1 to 26.5), so the queries run against the
columns iOS really ships. The unsent row copies one from the public
pub-hickman-16.1.2 and 17.3 backups: no text, no attributedBody, date_edited
set, date_retracted 0, and "rp" in message_summary_info.
"""

import json
import os
import plistlib
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.messages import MessageExtractor, _group_event  # noqa: E402

SCHEMAS = Path(__file__).resolve().parent.parent / "backupgen" / "schemas"
SCHEMA_VERSIONS = sorted(p.parent.name for p in SCHEMAS.glob("ios-*/sms.db.json"))
TABLES = ("message", "handle", "chat_handle_join", "chat_message_join",
          "attachment", "message_attachment_join")

KARIN, THEO, STRANGER = 1, 2, 3
CONTACTS = {"+15550001111": "Karin", "+15550002222": "Theo"}


class _FakeBackup:
    def __init__(self, sms_path: str):
        self._sms_path = sms_path

    def get_file(self, relative_path: str, domain: str = None):
        return self._sms_path if relative_path == "Library/SMS/sms.db" else None


def _summary(**info) -> bytes:
    return plistlib.dumps(info, fmt=plistlib.FMT_BINARY)


# message_id → (columns, expected message_type, expected event_text)
ROWS = {
    1: (dict(item_type=1, group_action_type=0, handle_id=KARIN, other_handle=THEO),
        "group_member_added", "Karin added Theo to the conversation"),
    2: (dict(item_type=1, group_action_type=1, is_from_me=1, handle_id=0, other_handle=THEO),
        "group_member_removed", "You removed Theo from the conversation"),
    3: (dict(item_type=2, is_from_me=1, handle_id=0, group_title="Trip"),
        "group_renamed", 'You named the conversation "Trip"'),
    4: (dict(item_type=2, handle_id=KARIN, group_title=None),
        "group_name_removed", "Karin removed the conversation name"),
    5: (dict(item_type=3, group_action_type=0, handle_id=THEO),
        "group_member_left", "Theo left the conversation"),
    6: (dict(item_type=3, group_action_type=1, handle_id=KARIN),
        "group_photo_changed", "Karin changed the group photo"),
    7: (dict(item_type=3, group_action_type=2, handle_id=STRANGER),
        "group_photo_removed", "+15550003333 removed the group photo"),
    # Messages' own "don't credit anyone" flag
    8: (dict(item_type=1, group_action_type=0, handle_id=KARIN, other_handle=THEO,
             message_summary_info=_summary(uat=True)),
        "group_member_added", "Theo was added to the conversation"),
    9: (dict(item_type=2, handle_id=KARIN, group_title="Trip", message_summary_info=_summary(uat=1)),
        "group_renamed", 'The conversation was named "Trip"'),
    # ...which never hides who left
    10: (dict(item_type=3, group_action_type=0, handle_id=THEO, message_summary_info=_summary(uat=True)),
         "group_member_left", "Theo left the conversation"),
    # No handle and not from me: say what happened without a name
    11: (dict(item_type=3, group_action_type=0, handle_id=0),
         "group_member_left", "Someone left the conversation"),
    12: (dict(item_type=3, group_action_type=1, handle_id=0),
         "group_photo_changed", "The group photo was changed"),
    13: (dict(item_type=1, group_action_type=0, handle_id=KARIN, other_handle=99),
         "group_member_added", "Karin added someone to the conversation"),
    # A group change that didn't go through
    14: (dict(item_type=1, group_action_type=0, handle_id=KARIN, other_handle=THEO, error=1),
         "group_member_added", "Karin added Theo to the conversation (failed)"),
    # Rows we can't name keep the type they always had
    15: (dict(item_type=1, group_action_type=0, handle_id=KARIN, other_handle=KARIN),
         "system", None),
    16: (dict(item_type=1, group_action_type=0, handle_id=KARIN, other_handle=0),
         "system", None),
    17: (dict(item_type=3, group_action_type=4, handle_id=KARIN),
         "location", None),
    18: (dict(item_type=5, handle_id=KARIN),
         "system", None),
    # Unsent, as real iOS 16/17 backups store it
    19: (dict(handle_id=KARIN, date_edited=727040269351320064,
              message_summary_info=_summary(ust=True, rp=[0], otr={"0": {"lo": 0, "le": 60}})),
         "unsent", "Karin unsent a message"),
    20: (dict(is_from_me=1, handle_id=KARIN, date_edited=727040269351320064,
              message_summary_info=_summary(ust=True, rp=[0], otr={"0": {"lo": 0, "le": 12}})),
         "unsent", "You unsent a message"),
    # Edited, not unsent: keeps its text
    21: (dict(text="Edited text", handle_id=KARIN, date_edited=727039885898427904,
              message_summary_info=_summary(ust=True, ep=[0], otr={"0": {"lo": 0, "le": 11}})),
         "text", None),
    # Unreadable summary info is ignored
    22: (dict(handle_id=KARIN, message_summary_info=b"not a plist"),
         "system", None),
    23: (dict(item_type=6, handle_id=KARIN, message_summary_info=_summary(conversationAVMode=2)),
         "facetime", "FaceTime call"),
    24: (dict(item_type=6, handle_id=KARIN, message_summary_info=_summary(conversationAVMode=1)),
         "facetime", "FaceTime call"),
    25: (dict(item_type=6, handle_id=KARIN),
         "facetime", "FaceTime call"),
    26: (dict(item_type=6, handle_id=KARIN, message_summary_info=_summary(conversationAVMode=0)),
         "facetime", "SharePlay session"),
    # Location sharing and ordinary messages are unchanged
    27: (dict(item_type=4, is_from_me=1, handle_id=0, share_direction=0, share_status=0),
         "location_started_by_me", None),
    28: (dict(item_type=4, handle_id=KARIN, share_direction=1, share_status=1),
         "location_stopped_by_them", None),
    29: (dict(text="See you at 6", handle_id=KARIN),
         "text", None),
}


def _build_sms_db(path: str, version: str, rows: dict) -> None:
    schema = json.loads((SCHEMAS / version / "sms.db.json").read_text(encoding="utf-8"))
    conn = sqlite3.connect(path)
    for obj in schema["objects"]:
        if obj["type"] == "table" and obj["name"] in TABLES:
            conn.execute(obj["sql"])
    columns = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
    handle_columns = {r[1] for r in conn.execute("PRAGMA table_info(handle)")}
    for rowid, number in ((KARIN, "+15550001111"), (THEO, "+15550002222"), (STRANGER, "+15550003333")):
        handle = {"ROWID": rowid, "id": number, "service": "iMessage"}
        if "uncanonicalized_id" in handle_columns:
            handle["uncanonicalized_id"] = number
        conn.execute(f"INSERT INTO handle ({', '.join(handle)}) VALUES ({', '.join('?' * len(handle))})",
                     list(handle.values()))
        conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, ?)", (rowid,))
    for message_id, values in rows.items():
        row = {"ROWID": message_id, "guid": f"guid-{message_id}", "date": message_id * 1_000_000_000,
               "is_from_me": 0, "text": None, **values}
        row = {k: v for k, v in row.items() if k in columns}
        conn.execute(f"INSERT INTO message ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, ?)", (message_id,))
    conn.commit()
    conn.close()


@pytest.fixture(params=SCHEMA_VERSIONS)
def messages(request):
    with tempfile.TemporaryDirectory() as tmp:
        sms_path = os.path.join(tmp, "sms.db")
        rows = {mid: values for mid, (values, _, _) in ROWS.items()}
        # date_retracted (iOS 16+) on its own also means unsent
        rows[30] = dict(handle_id=THEO, date_retracted=727040269351320064)
        _build_sms_db(sms_path, request.param, rows)
        extractor = MessageExtractor()
        try:
            result = extractor.get_messages(_FakeBackup(sms_path), chat_id=1, contacts=CONTACTS, limit=100)
            with closing(sqlite3.connect(sms_path)) as conn:
                columns = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
        finally:
            extractor.close()
        assert "error" not in result
        yield {m["message_id"]: m for m in result["messages"]}, columns


def test_schemas_were_found():
    assert "ios-13.3.1" in SCHEMA_VERSIONS and "ios-26.5" in SCHEMA_VERSIONS


@pytest.mark.parametrize("message_id", sorted(ROWS))
def test_each_row_is_named(messages, message_id):
    by_id, _ = messages
    _, message_type, event_text = ROWS[message_id]
    msg = by_id[message_id]
    assert (msg["message_type"], msg["event_text"]) == (message_type, event_text)


def test_date_retracted_alone_means_unsent(messages):
    by_id, columns = messages
    if "date_retracted" not in columns:
        pytest.skip("date_retracted arrived in iOS 16")
    assert (by_id[30]["message_type"], by_id[30]["event_text"]) == ("unsent", "Theo unsent a message")


def test_events_keep_sender_fields(messages):
    by_id, _ = messages
    assert by_id[1]["sender"] == "Karin" and by_id[1]["sender_handle"] == "+15550001111"
    assert by_id[3]["sender"] == "me" and by_id[3]["is_from_me"] is True
    assert not by_id[1]["text"] and by_id[1]["has_attachments"] is False


def test_contact_found_by_uncanonicalized_id():
    contacts = {"(555) 000-2222": "Theo B"}
    with tempfile.TemporaryDirectory() as tmp:
        sms_path = os.path.join(tmp, "sms.db")
        _build_sms_db(sms_path, "ios-26.5", {1: ROWS[1][0]})
        conn = sqlite3.connect(sms_path)
        conn.execute("UPDATE handle SET uncanonicalized_id = '(555) 000-2222' WHERE ROWID = ?", (THEO,))
        conn.commit()
        conn.close()
        extractor = MessageExtractor()
        try:
            msg = extractor.get_messages(_FakeBackup(sms_path), chat_id=1, contacts=contacts)["messages"][0]
        finally:
            extractor.close()
    assert msg["event_text"] == "+15550001111 added Theo B to the conversation"


def test_group_event_words():
    assert _group_event(3, 0, "", "", "", False) == ("group_member_left", "Someone left the conversation")
    assert _group_event(2, 7, "Karin", "", "Trip", False) == ("group_renamed", 'Karin named the conversation "Trip"')
    assert _group_event(3, 3, "Karin", "", "", False) is None
    assert _group_event(1, 2, "Karin", "Theo", "", False) is None
