"""Which list a voicemail is in, and voicemail kept in more than one store.

The flags were checked against an iOS 27.2 iPhone's Phone app, one voicemail at
a time (2026-11-16): a voicemail with a trashed_date was in Deleted, one with
0x8 and no trashed_date was in Deleted too, one with 0x80 was in Spam, and the
oldest in the main list was a carrier-less one (remote_uid 0, 0x40). The rows
below use the flags that phone's backup held.

OE-79 (iOS 27.2) listed spam among the voicemails; OE-85 (iOS 18.7.2) listed
three the phone didn't show and missed the one it did.
"""

import os
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.calls import CallExtractor  # noqa: E402
from ios_backup_core.extractors.voicemail import (  # noqa: E402
    VoicemailExtractor,
    folder_of,
    is_deleted,
    is_spam,
)

MAIN = "Library/Voicemail/voicemail.db"
ACCOUNT = "A-0B1C2D3E-4F50-4A6B-8C7D-9E0F1A2B3C4D"
ACCOUNT_DB = f"Library/Voicemail/{ACCOUNT}/voicemail.db"

LIVE = 0x1103        # heard, transcribed
UNHEARD = 0x1102
OLD_CARRIER = 0x1143  # remote_uid 0: the carrier no longer has it, still in the main list
SPAM = 0x1182
TRASHED = 0x110A      # always with a trashed_date
DELETED_LONG_AGO = 0x110B  # 0x8 and no trashed_date: in Deleted on the phone


class _Backup:
    """Accessor stub: ``files`` maps relative paths to local files."""

    encrypted = False

    def __init__(self, files):
        self.files = dict(files)

    def get_file(self, relative_path, domain="HomeDomain"):
        return self.files.get(relative_path)

    def list_files(self, domain=None, path_like=None):
        paths = list(self.files)
        if path_like:
            pattern = ".*".join(re.escape(p) for p in path_like.split("%"))
            paths = [p for p in paths if re.fullmatch(pattern, p, re.IGNORECASE)]
        return [{"hash": "", "domain": "HomeDomain", "path": p} for p in paths]


def _store(path, rows):
    """rows: (ROWID, remote_uid, date, sender, duration, flags, trashed_date)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE voicemail (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, remote_uid INTEGER, "
        "date INTEGER, token TEXT, sender TEXT, callback_num TEXT, duration INTEGER, "
        "expiration INTEGER, trashed_date INTEGER, flags INTEGER, receiver TEXT, label TEXT)"
    )
    conn.executemany(
        "INSERT INTO voicemail (ROWID, remote_uid, date, sender, duration, flags, trashed_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
    )
    conn.commit()
    conn.close()
    return str(path)


def _audio(tmp_path, folder, *rowids):
    files = {}
    for rowid in rowids:
        local = tmp_path / "audio" / folder.replace("/", "_") / f"{rowid}.amr"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"#!AMR\n" + bytes([rowid % 256]))
        files[f"{folder}/{rowid}.amr"] = str(local)
    return files


def _list(backup, **kwargs):
    result = VoicemailExtractor().list_voicemails(backup, contacts={}, **kwargs)
    assert "error" not in result, result.get("error")
    return result


@pytest.mark.parametrize("flags, trashed_date, folder", [
    (LIVE, 0, "voicemail"),
    (UNHEARD, None, "voicemail"),
    (OLD_CARRIER, 0, "voicemail"),
    (3, 0, "voicemail"),
    (SPAM, 0, "spam"),
    (0x82, 0, "spam"),
    (TRASHED, 795000000, "deleted"),
    (DELETED_LONG_AGO, 0, "deleted"),
    (0x118A, 795000000, "deleted"),  # spam that was then deleted: in Deleted, not Spam
    (0, 795000000, "deleted"),
    (None, None, "voicemail"),
])
def test_folder_follows_the_phone(flags, trashed_date, folder):
    assert folder_of(flags, trashed_date) == folder


@pytest.mark.parametrize("flags, trashed_date, spam, deleted", [
    (LIVE, 0, False, False),
    (SPAM, 0, True, False),
    (TRASHED, 795000000, False, True),
    (DELETED_LONG_AGO, 0, False, True),
    (0x118A, 795000000, True, True),  # spam, then deleted: both marks
    (0x1188, 0, True, True),          # the same without a trashed_date
    (0x80, None, True, False),
    (0, 795000000, False, True),
    (None, None, False, False),
])
def test_spam_and_deleted_are_separate_marks(flags, trashed_date, spam, deleted):
    assert (is_spam(flags), is_deleted(flags, trashed_date)) == (spam, deleted)


@pytest.fixture
def one_store(tmp_path):
    db = _store(tmp_path / "main" / "voicemail.db", [
        (1, 0, 1_500_000_000, "+15550000001", 20, OLD_CARRIER, 0),
        (2, 0, 1_510_000_000, "+15550000002", 21, DELETED_LONG_AGO, 0),
        (3, 316, 1_700_000_000, "+15550000003", 22, TRASHED, 1_700_000_500),
        (4, 9001, 1_760_000_000, "+15550000004", 23, SPAM, 0),
        (5, 9002, 1_770_000_000, "+15550000005", 24, LIVE, 0),
    ])
    return _Backup({MAIN: db, **_audio(tmp_path, "Library/Voicemail", 1, 2, 3, 4, 5)})


def test_deleted_are_left_out_unless_asked_for(one_store):
    result = _list(one_store)
    assert [(v["id"], v["folder"]) for v in result["voicemails"]] == [(5, "voicemail"), (4, "spam"), (1, "voicemail")]
    assert result["counts"] == {"voicemail": 2, "spam": 1, "deleted": 2}


def test_every_list_when_deleted_are_asked_for(one_store):
    result = _list(one_store, include_deleted=True)
    assert [(v["id"], v["folder"]) for v in result["voicemails"]] == [
        (5, "voicemail"), (4, "spam"), (3, "deleted"), (2, "deleted"), (1, "voicemail"),
    ]
    assert result["counts"] == {"voicemail": 2, "spam": 1, "deleted": 2}


def test_each_voicemail_says_spam_and_deleted(one_store):
    result = _list(one_store, include_deleted=True)
    assert [(v["id"], v["folder"], v["is_spam"], v["is_deleted"]) for v in result["voicemails"]] == [
        (5, "voicemail", False, False),
        (4, "spam", True, False),
        (3, "deleted", False, True),
        (2, "deleted", False, True),
        (1, "voicemail", False, False),
    ]


def test_a_deleted_spam_voicemail_keeps_its_spam_mark(tmp_path):
    db = _store(tmp_path / "main" / "voicemail.db", [
        (1, 9001, 1_760_000_000, "+15550000004", 23, SPAM, 0),
        (2, 9002, 1_761_000_000, "+15550000006", 11, 0x118A, 1_761_000_900),  # spam, then deleted
        (3, 9003, 1_762_000_000, "+15550000007", 12, 0x1188, 0),               # likewise, no trashed_date
    ])
    result = _list(_Backup({MAIN: db}), include_deleted=True)
    assert [(v["id"], v["folder"], v["is_spam"], v["is_deleted"]) for v in result["voicemails"]] == [
        (3, "deleted", True, True),
        (2, "deleted", True, True),
        (1, "spam", True, False),
    ]
    assert result["counts"] == {"voicemail": 0, "spam": 1, "deleted": 2}


def test_a_saved_contacts_number_is_kept(tmp_path):
    """contact_name is the address book's name; phone_number stays the number."""
    db = _store(tmp_path / "main" / "voicemail.db", [
        (1, 9001, 1_760_000_000, "+15550000004", 23, SPAM, 0),
        (2, 9002, 1_761_000_000, "+15550000099", 11, LIVE, 0),
    ])
    result = VoicemailExtractor().list_voicemails(_Backup({MAIN: db}), contacts={"+15550000004": "Dentist"})
    assert [(v["phone_number"], v["contact_name"]) for v in result["voicemails"]] == [
        ("+15550000099", "+15550000099"),
        ("+15550000004", "Dentist"),
    ]


def test_only_deleted_ones_is_no_records_with_counts(tmp_path):
    db = _store(tmp_path / "main" / "voicemail.db", [(1, 5, 1_700_000_000, "+15550000001", 9, TRASHED, 1_700_000_100)])
    result = _list(_Backup({MAIN: db}))
    assert result["voicemails"] == []
    assert result["empty_reason"] == "no_records"
    assert result["counts"] == {"voicemail": 0, "spam": 0, "deleted": 1}


def _two_stores(tmp_path):
    """The layout of the iOS 27.2 backup: the account store repeats the main
    store's newest rows under other ROWIDs and remote_uids, and adds one."""
    main = _store(tmp_path / "main" / "voicemail.db", [
        (2400, 5001, 1_773_000_000, "+15550000010", 12, SPAM, 0),
        (2402, 5002, 1_776_000_000, "+15550000011", 20, LIVE, 0),
        (2404, 5003, 1_787_000_000, "+15550000012", 4, TRASHED, 1_787_400_000),
        (2405, 5004, 1_791_000_000, "+15550000013", 43, LIVE, 0),
    ])
    account = _store(tmp_path / "acct" / "voicemail.db", [
        (4, 5001, 1_773_000_000, "+15550000010", 12, 0xC2, 0),
        (6, 5002, 1_776_000_000, "+15550000011", 20, 0x43, 0),
        (8, 6003, 1_787_000_000, "+15550000012", 4, 0x4A, 1_787_400_000),
        (9, 5004, 1_791_000_000, "+15550000013", 43, 0x3, 0),
        (10, 6005, 1_792_000_000, "+15550000014", 31, 0x2, 0),  # only here
    ])
    return {
        MAIN: main, ACCOUNT_DB: account,
        **_audio(tmp_path, "Library/Voicemail", 2400, 2402, 2404, 2405),
        **_audio(tmp_path, f"Library/Voicemail/{ACCOUNT}", 4, 6, 8, 9, 10),
    }


def test_a_voicemail_in_two_stores_is_listed_once(tmp_path):
    result = _list(_Backup(_two_stores(tmp_path)), include_deleted=True)
    assert [(v["id"], v["folder"]) for v in result["voicemails"]] == [
        (f"{ACCOUNT}.10", "voicemail"), (2405, "voicemail"), (2404, "deleted"), (2402, "voicemail"), (2400, "spam"),
    ]
    assert result["counts"] == {"voicemail": 3, "spam": 1, "deleted": 1}


def test_a_voicemail_only_in_the_account_store_plays(tmp_path):
    backup = _Backup(_two_stores(tmp_path))
    path = VoicemailExtractor().get_audio_path(backup, f"{ACCOUNT}.10")
    assert path == backup.files[f"Library/Voicemail/{ACCOUNT}/10.amr"]


def test_the_copy_with_audio_is_the_one_listed(tmp_path):
    files = _two_stores(tmp_path)
    del files["Library/Voicemail/2405.amr"]  # the main store's copy lost its audio
    backup = _Backup(files)
    newest_two = [v["id"] for v in _list(backup)["voicemails"][:2]]
    assert newest_two == [f"{ACCOUNT}.10", f"{ACCOUNT}.9"]
    assert VoicemailExtractor().get_audio_path(backup, f"{ACCOUNT}.9")


def test_deleted_in_one_store_is_deleted(tmp_path):
    files = _two_stores(tmp_path)
    account = files[ACCOUNT_DB]
    conn = sqlite3.connect(account)
    conn.execute("UPDATE voicemail SET flags = 0x4A, trashed_date = 1792000100 WHERE ROWID = 10")
    conn.execute("UPDATE voicemail SET flags = 0x4B, trashed_date = 1792000100 WHERE ROWID = 9")
    conn.commit()
    conn.close()
    result = _list(_Backup(files))
    assert [v["id"] for v in result["voicemails"]] == [2402, 2400]
    assert result["counts"] == {"voicemail": 1, "spam": 1, "deleted": 3}


def test_spam_in_one_store_and_deleted_in_another_is_both(tmp_path):
    files = _two_stores(tmp_path)
    conn = sqlite3.connect(files[ACCOUNT_DB])
    # The account store's copy of the main store's spam 2400 is deleted, and not marked spam
    conn.execute("UPDATE voicemail SET flags = 0x4A, trashed_date = 1773000500 WHERE ROWID = 4")
    conn.commit()
    conn.close()
    result = _list(_Backup(files), include_deleted=True)
    by_id = {v["id"]: v for v in result["voicemails"]}
    assert (by_id[2400]["folder"], by_id[2400]["is_spam"], by_id[2400]["is_deleted"]) == ("deleted", True, True)


def test_rows_say_spam_and_deleted_for_other_readers(tmp_path):
    rows = VoicemailExtractor().voicemail_rows(_Backup(_two_stores(tmp_path)))["rows"]
    assert {(r["id"], r["is_spam"], r["is_deleted"]) for r in rows} == {
        (f"{ACCOUNT}.10", False, False), (2405, False, False), (2404, False, True),
        (2402, False, False), (2400, True, False),
    }


def test_twins_within_one_store_stay_apart(tmp_path):
    """The main store can hold two rows for one message (same date, length and
    caller, other remote_uids). The phone counts both, so do we."""
    db = _store(tmp_path / "main" / "voicemail.db", [
        (2399, 7001, 1_774_000_000, "+15550000020", 7, TRASHED, 1_774_000_600),
        (2401, 7002, 1_774_000_000, "+15550000020", 7, TRASHED, 1_774_000_600),
    ])
    result = _list(_Backup({MAIN: db}), include_deleted=True)
    assert [v["id"] for v in result["voicemails"]] == [2399, 2401]


def test_an_account_store_alone_is_read(tmp_path):
    account = _store(tmp_path / "acct" / "voicemail.db", [(3, 7, 1_790_000_000, "+15550000030", 15, 0x3, 0)])
    result = _list(_Backup({ACCOUNT_DB: account}))
    assert [v["id"] for v in result["voicemails"]] == [f"{ACCOUNT}.3"]


def test_an_unreadable_account_store_leaves_the_main_one_listed(tmp_path):
    files = _two_stores(tmp_path)
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all" * 100)
    files[ACCOUNT_DB] = str(broken)
    result = _list(_Backup(files))
    assert [v["id"] for v in result["voicemails"]] == [2405, 2402, 2400]


@pytest.mark.parametrize("voicemail_id", [
    "../../../etc/passwd", "A-x/../1", "1.amr", "", "A-ABC.", "A-ABC.1/2", None, "-1",
])
def test_ids_that_arent_voicemails_find_nothing(tmp_path, voicemail_id):
    backup = _Backup(_two_stores(tmp_path))
    assert VoicemailExtractor().get_audio_path(backup, voicemail_id) is None


def test_an_old_number_id_still_plays(one_store):
    assert VoicemailExtractor().get_audio_path(one_store, 5) == one_store.files["Library/Voicemail/5.amr"]
    assert VoicemailExtractor().get_audio_path(one_store, "5") == one_store.files["Library/Voicemail/5.amr"]


def test_calls_get_each_voicemail_once_and_no_deleted_ones(tmp_path):
    calls = CallExtractor()._read_voicemails_as_calls(_Backup(_two_stores(tmp_path)), {})
    assert sorted(c["call_id"] for c in calls) == sorted(
        ["vm_2400", "vm_2402", "vm_2405", f"vm_{ACCOUNT}.10"]
    )


def test_a_schema_without_some_columns_still_lists(tmp_path):
    """Old or pared-down voicemail.db files can lack duration, flags or trashed_date."""
    path = tmp_path / "main" / "voicemail.db"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE voicemail (ROWID INTEGER PRIMARY KEY, sender TEXT, date INTEGER)")
    conn.execute("INSERT INTO voicemail VALUES (1, '+15550000040', 1700000000)")
    conn.commit()
    conn.close()
    result = _list(_Backup({MAIN: str(path)}))
    assert [(v["id"], v["folder"], v["duration"], v["is_read"], v["is_spam"], v["is_deleted"])
            for v in result["voicemails"]] == [(1, "voicemail", 0, False, False, False)]
