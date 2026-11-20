"""Who a note is shared with, from the CKShare in ZSERVERSHAREDATA.

The archive layout follows apple_cloud_notes_parser (threeplanetssoftware), which
reads Participants → UserIdentity → LookupInfo / NameComponents from the same
column. No real backup with a shared note has been checked against this yet.
"""

import os
import plistlib
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backupgen.persona import Contact  # noqa: E402
from backupgen.writers.notes import share_address  # noqa: E402
from ios_backup_core.extractors.notes import NoteExtractor, share_participants  # noqa: E402


def archive(participants, wrap_in_root=False, extra_top=None):
    """A CKShare archive as NSKeyedArchiver writes it. Each participant is a dict of
    given/family/nickname/email/phone/record, or None for one with no UserIdentity."""
    objects = ["$null"]

    def add(obj):
        objects.append(obj)
        return plistlib.UID(len(objects) - 1)

    def string(value):
        return add(value) if value is not None else plistlib.UID(0)

    members = []
    for p in participants:
        if p is None:
            members.append(add({"Role": 3}))
            continue
        names = add({"NS.givenName": string(p.get("given")), "NS.familyName": string(p.get("family")),
                     "NS.nickname": string(p.get("nickname"))})
        identity = add({
            "LookupInfo": add({"EmailAddress": string(p.get("email")), "PhoneNumber": string(p.get("phone"))}),
            "NameComponents": add({"NS.nameComponentsPrivate": names}),
            "UserRecordID": add({"RecordName": string(p.get("record"))}),
        })
        members.append(add({"UserIdentity": identity, "Role": p.get("role", 3)}))
    share = {"Participants": add({"NS.objects": members}), **(extra_top or {})}
    top = {"root": add(share)} if wrap_in_root else share
    return plistlib.dumps({"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": top, "$objects": objects},
                          fmt=plistlib.FMT_BINARY)


OWNER = {"given": "Maren", "family": "Lindqvist", "email": "maren@example.com", "record": "__defaultOwner__", "role": 1}
ERIK = {"given": "Erik", "family": "Lindqvist", "email": "erik@example.com", "record": "_e1"}
JESS = {"given": "Jess", "family": "Moreno", "phone": "+12085550146", "record": "_j1"}


def test_everyone_but_the_phones_owner():
    blob = archive([OWNER, ERIK, JESS])
    assert share_participants(blob) == [
        {"name": "Erik Lindqvist", "email": "erik@example.com", "phone": None},
        {"name": "Jess Moreno", "email": None, "phone": "+12085550146"},
    ]


def test_the_owner_by_their_notes_account_record_name():
    # Someone else shared it: they're the share's owner, and the phone's owner is a
    # participant under the record name on their Notes account
    me = {**OWNER, "record": "_me", "role": 3}
    blob = archive([{**ERIK, "role": 1}, me])
    assert share_participants(blob, frozenset({"_me"})) == [
        {"name": "Erik Lindqvist", "email": "erik@example.com", "phone": None},
    ]
    # Without knowing the account, the phone's owner is listed too
    assert [p["name"] for p in share_participants(blob)] == ["Erik Lindqvist", "Maren Lindqvist"]


def test_share_under_a_root_key_and_names_without_parts():
    blob = archive([OWNER, {"nickname": "Bean", "phone": "+15035550199"}], wrap_in_root=True)
    assert share_participants(blob) == [{"name": "Bean", "email": None, "phone": "+15035550199"}]


def test_participants_with_nothing_to_go_on_are_left_out():
    blob = archive([OWNER, None, {"record": "_x"}, ERIK, ERIK])
    assert share_participants(blob) == [{"name": "Erik Lindqvist", "email": "erik@example.com", "phone": None}]


def test_unreadable_share_data_is_no_one():
    assert share_participants(None) == []
    assert share_participants(b"") == []
    assert share_participants(b"not a plist") == []
    assert share_participants(plistlib.dumps({"$objects": "nope"}, fmt=plistlib.FMT_BINARY)) == []
    # No Participants at all, and a UID pointing past the end of the archive
    assert share_participants(archive([], extra_top={})) == []
    broken = plistlib.dumps({"$archiver": "NSKeyedArchiver", "$top": {"Participants": plistlib.UID(99)},
                             "$objects": ["$null"]}, fmt=plistlib.FMT_BINARY)
    assert share_participants(broken) == []


def note_store(path, rows):
    """A NoteStore.sqlite with just the columns list_notes reads."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (
            Z_PK INTEGER PRIMARY KEY, ZTITLE1 TEXT, ZTITLE2 TEXT, ZNAME TEXT, ZSNIPPET TEXT, ZFOLDER INTEGER,
            ZCREATIONDATE1 REAL, ZMODIFICATIONDATE1 REAL, ZSERVERSHAREDATA BLOB, ZUSERRECORDNAME TEXT);
        CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZNOTE INTEGER, ZDATA BLOB);
    """)
    for row in rows:
        keys = ", ".join(row)
        conn.execute(f"INSERT INTO ZICCLOUDSYNCINGOBJECT ({keys}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    conn.commit()
    conn.close()
    return str(path)


def test_a_note_gets_its_own_share_and_its_folders(tmp_path):
    me = {**OWNER, "record": "_me"}
    path = note_store(tmp_path / "NoteStore.sqlite", [
        {"Z_PK": 1, "ZNAME": "iCloud", "ZUSERRECORDNAME": "_me"},
        {"Z_PK": 2, "ZTITLE2": "Family", "ZSERVERSHAREDATA": archive([me, ERIK])},
        {"Z_PK": 3, "ZTITLE2": "Notes"},
        {"Z_PK": 10, "ZTITLE1": "Wedding flowers", "ZFOLDER": 2, "ZMODIFICATIONDATE1": 3.0,
         "ZSERVERSHAREDATA": archive([me, JESS, ERIK])},
        {"Z_PK": 11, "ZTITLE1": "Thanksgiving", "ZFOLDER": 2, "ZMODIFICATIONDATE1": 2.0},
        {"Z_PK": 12, "ZTITLE1": "Groceries", "ZFOLDER": 3, "ZMODIFICATIONDATE1": 1.0},
        {"Z_PK": 13, "ZTITLE1": "Garbled", "ZFOLDER": 3, "ZMODIFICATIONDATE1": 0.5, "ZSERVERSHAREDATA": b"\x00junk"},
    ])
    notes = {n["title"]: n for n in NoteExtractor()._parse_notestore(path)}
    assert [p["name"] for p in notes["Wedding flowers"]["shared_with"]] == ["Jess Moreno", "Erik Lindqvist"]
    assert [p["name"] for p in notes["Thanksgiving"]["shared_with"]] == ["Erik Lindqvist"]
    assert notes["Groceries"]["shared_with"] == []
    assert notes["Garbled"]["shared_with"] == []


def test_a_store_without_share_columns(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.sqlite")
    conn.executescript("""
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER PRIMARY KEY, ZTITLE1 TEXT, ZSNIPPET TEXT,
            ZCREATIONDATE1 REAL, ZMODIFICATIONDATE1 REAL);
        CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZNOTE INTEGER, ZDATA BLOB);
        INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (1, 'Old note', 'hi', 1.0, 1.0);
    """)
    conn.commit()
    conn.close()
    [note] = NoteExtractor()._parse_notestore(str(tmp_path / "old.sqlite"))
    assert (note["title"], note["shared_with"]) == ("Old note", [])


def test_backupgen_shares_by_first_email_else_first_phone():
    assert share_address(Contact("erik", "Erik", "Lindqvist", phones=[("mobile", "+1 208 555 0110")],
                                 emails=[("home", "Erik.Lindqvist@example.com")])) == \
        {"name": "Erik Lindqvist", "email": "erik.lindqvist@example.com", "phone": None}
    assert share_address(Contact("jess", "Jess", "Moreno", phones=[("mobile", "+1 208 555 0146")])) == \
        {"name": "Jess Moreno", "email": None, "phone": "+12085550146"}
