"""Which list a voicemail is in, and voicemail kept in more than one store.

A voicemail with a trashed_date is in Deleted, so is one with 0x8 and no
trashed_date, and one with 0x80 is in Spam. Every value below is made up. Besides
the bits the reader knows (0x1 heard, 0x8 deleted, 0x80 spam, and 0x2), the flag
words carry a bit nothing here gives a meaning to (0x2000), so the reader is seen
to ignore it.

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

LIVE = 0x2003        # heard, plus a bit with no meaning here
UNHEARD = 0x2002
SPAM = 0x2082
TRASHED = 0x200A      # always with a trashed_date
DELETED_LONG_AGO = 0x200B  # 0x8 and no trashed_date: in Deleted
SPAM_TRASHED = 0x208A
SPAM_DELETED = 0x2088


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
    (3, 0, "voicemail"),
    (SPAM, 0, "spam"),
    (0x82, 0, "spam"),
    (TRASHED, 720000000, "deleted"),
    (DELETED_LONG_AGO, 0, "deleted"),
    (SPAM_TRASHED, 720000000, "deleted"),  # spam that was then deleted: in Deleted, not Spam
    (0, 720000000, "deleted"),
    (None, None, "voicemail"),
])
def test_folder_follows_the_phone(flags, trashed_date, folder):
    assert folder_of(flags, trashed_date) == folder


@pytest.mark.parametrize("flags, trashed_date, spam, deleted", [
    (LIVE, 0, False, False),
    (SPAM, 0, True, False),
    (TRASHED, 720000000, False, True),
    (DELETED_LONG_AGO, 0, False, True),
    (SPAM_TRASHED, 720000000, True, True),  # spam, then deleted: both marks
    (SPAM_DELETED, 0, True, True),          # the same without a trashed_date
    (0x80, None, True, False),
    (0, 720000000, False, True),
    (None, None, False, False),
])
def test_spam_and_deleted_are_separate_marks(flags, trashed_date, spam, deleted):
    assert (is_spam(flags), is_deleted(flags, trashed_date)) == (spam, deleted)


@pytest.fixture
def one_store(tmp_path):
    db = _store(tmp_path / "main" / "voicemail.db", [
        (1, 0, 1_500_000_000, "+15550000001", 50, LIVE, 0),
        (2, 0, 1_510_000_000, "+15550000002", 51, DELETED_LONG_AGO, 0),
        (3, 9003, 1_700_000_000, "+15550000003", 52, TRASHED, 1_700_000_500),
        (4, 9001, 1_700_200_000, "+15550000004", 53, SPAM, 0),
        (5, 9002, 1_700_300_000, "+15550000005", 54, LIVE, 0),
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
        (1, 9001, 1_700_000_000, "+15550000004", 53, SPAM, 0),
        (2, 9002, 1_701_000_000, "+15550000006", 41, SPAM_TRASHED, 1_701_000_900),  # spam, then deleted
        (3, 9003, 1_702_000_000, "+15550000007", 42, SPAM_DELETED, 0),              # likewise, no trashed_date
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
        (1, 9001, 1_700_000_000, "+15550000004", 53, SPAM, 0),
        (2, 9002, 1_701_000_000, "+15550000099", 41, LIVE, 0),
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
    """An account store that repeats the main store's rows under other ROWIDs and
    remote_uids, and adds one."""
    main = _store(tmp_path / "main" / "voicemail.db", [
        (21, 5001, 1_700_000_000, "+15550000010", 12, SPAM, 0),
        (22, 5002, 1_700_100_000, "+15550000011", 25, LIVE, 0),
        (23, 5003, 1_700_200_000, "+15550000012", 6, TRASHED, 1_700_250_000),
        (24, 5004, 1_700_300_000, "+15550000013", 30, LIVE, 0),
    ])
    account = _store(tmp_path / "acct" / "voicemail.db", [
        (1, 6001, 1_700_000_000, "+15550000010", 12, LIVE, 0),
        (2, 6002, 1_700_100_000, "+15550000011", 25, LIVE, 0),
        (3, 6003, 1_700_200_000, "+15550000012", 6, LIVE, 0),
        (12, 6004, 1_700_300_000, "+15550000013", 30, LIVE, 0),
        (13, 6005, 1_700_400_000, "+15550000014", 31, LIVE, 0),  # only here
    ])
    return {
        MAIN: main, ACCOUNT_DB: account,
        **_audio(tmp_path, "Library/Voicemail", 21, 22, 23, 24),
        **_audio(tmp_path, f"Library/Voicemail/{ACCOUNT}", 1, 2, 3, 12, 13),
    }


def test_a_voicemail_in_two_stores_is_listed_once(tmp_path):
    result = _list(_Backup(_two_stores(tmp_path)), include_deleted=True)
    assert [(v["id"], v["folder"]) for v in result["voicemails"]] == [
        (f"{ACCOUNT}.13", "voicemail"), (24, "voicemail"), (23, "deleted"), (22, "voicemail"), (21, "spam"),
    ]
    assert result["counts"] == {"voicemail": 3, "spam": 1, "deleted": 1}


def test_a_voicemail_only_in_the_account_store_plays(tmp_path):
    backup = _Backup(_two_stores(tmp_path))
    path = VoicemailExtractor().get_audio_path(backup, f"{ACCOUNT}.13")
    assert path == backup.files[f"Library/Voicemail/{ACCOUNT}/13.amr"]


def test_the_copy_with_audio_is_the_one_listed(tmp_path):
    files = _two_stores(tmp_path)
    del files["Library/Voicemail/24.amr"]  # the main store's copy lost its audio
    backup = _Backup(files)
    newest_two = [v["id"] for v in _list(backup)["voicemails"][:2]]
    assert newest_two == [f"{ACCOUNT}.13", f"{ACCOUNT}.12"]
    assert VoicemailExtractor().get_audio_path(backup, f"{ACCOUNT}.12")


def test_deleted_in_one_store_is_deleted(tmp_path):
    files = _two_stores(tmp_path)
    account = files[ACCOUNT_DB]
    conn = sqlite3.connect(account)
    conn.execute("UPDATE voicemail SET flags = ?, trashed_date = 1700400100 WHERE ROWID IN (12, 13)", (TRASHED,))
    conn.commit()
    conn.close()
    result = _list(_Backup(files))
    assert [v["id"] for v in result["voicemails"]] == [22, 21]
    assert result["counts"] == {"voicemail": 1, "spam": 1, "deleted": 3}


def test_spam_in_one_store_and_deleted_in_another_is_both(tmp_path):
    files = _two_stores(tmp_path)
    conn = sqlite3.connect(files[ACCOUNT_DB])
    # The account store's copy of the main store's spam 21 is deleted, and not marked spam
    conn.execute("UPDATE voicemail SET flags = ?, trashed_date = 1700000500 WHERE ROWID = 1", (TRASHED,))
    conn.commit()
    conn.close()
    result = _list(_Backup(files), include_deleted=True)
    by_id = {v["id"]: v for v in result["voicemails"]}
    assert (by_id[21]["folder"], by_id[21]["is_spam"], by_id[21]["is_deleted"]) == ("deleted", True, True)


def test_rows_say_spam_and_deleted_for_other_readers(tmp_path):
    rows = VoicemailExtractor().voicemail_rows(_Backup(_two_stores(tmp_path)))["rows"]
    assert {(r["id"], r["is_spam"], r["is_deleted"]) for r in rows} == {
        (f"{ACCOUNT}.13", False, False), (24, False, False), (23, False, True),
        (22, False, False), (21, True, False),
    }


def test_twins_within_one_store_stay_apart(tmp_path):
    """Two rows in the main store with the same date, length and caller (other
    remote_uids) are two voicemails."""
    db = _store(tmp_path / "main" / "voicemail.db", [
        (41, 7001, 1_700_500_000, "+15550000020", 9, TRASHED, 1_700_500_600),
        (43, 7002, 1_700_500_000, "+15550000020", 9, TRASHED, 1_700_500_600),
    ])
    result = _list(_Backup({MAIN: db}), include_deleted=True)
    assert [v["id"] for v in result["voicemails"]] == [41, 43]


def test_twins_each_keep_their_own_copy(tmp_path):
    """Two rows for one message in the main store, each with a copy in the account store: still two, not three."""
    main = _store(tmp_path / "main" / "voicemail.db", [
        (41, 7001, 1_700_500_000, "+15550000020", 9, LIVE, 0),
        (43, 7002, 1_700_500_000, "+15550000020", 9, LIVE, 0),
    ])
    account = _store(tmp_path / "acct" / "voicemail.db", [
        (1, 8001, 1_700_500_000, "+15550000020", 9, 0x3, 0),
        (2, 8002, 1_700_500_000, "+15550000020", 9, 0x3, 0),
    ])
    files = {MAIN: main, ACCOUNT_DB: account, **_audio(tmp_path, "Library/Voicemail", 41, 43),
             **_audio(tmp_path, f"Library/Voicemail/{ACCOUNT}", 1, 2)}
    assert [v["id"] for v in _list(_Backup(files))["voicemails"]] == [41, 43]


def test_an_account_store_alone_is_read(tmp_path):
    account = _store(tmp_path / "acct" / "voicemail.db", [(3, 7, 1_700_600_000, "+15550000030", 15, LIVE, 0)])
    result = _list(_Backup({ACCOUNT_DB: account}))
    assert [v["id"] for v in result["voicemails"]] == [f"{ACCOUNT}.3"]


def test_an_unreadable_account_store_leaves_the_main_one_listed(tmp_path):
    files = _two_stores(tmp_path)
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all" * 100)
    files[ACCOUNT_DB] = str(broken)
    result = _list(_Backup(files))
    assert [v["id"] for v in result["voicemails"]] == [24, 22, 21]


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
        ["vm_21", "vm_22", "vm_24", f"vm_{ACCOUNT}.13"]
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


@pytest.mark.parametrize("copy_date, copy_sender", [
    (1_700_000_000, "15550000013"),      # the number without its "+"
    (1_700_000_001, "+15550000013"),     # a second later
    (1_699_999_999, "+15550000013"),     # a second earlier
    (1_700_000_001, "15550000013"),      # both
])
def test_a_copy_a_little_different_is_still_one_voicemail(tmp_path, copy_date, copy_sender):
    """A line's copy can give the number without its "+" or a date a second off; it's the same voicemail."""
    main = _store(tmp_path / "main" / "voicemail.db", [(31, 5004, 1_700_000_000, "+15550000013", 30, LIVE, 0)])
    account = _store(tmp_path / "acct" / "voicemail.db", [(3, 6004, copy_date, copy_sender, 30, 0x3, 0)])
    files = {MAIN: main, ACCOUNT_DB: account, **_audio(tmp_path, "Library/Voicemail", 31),
             **_audio(tmp_path, f"Library/Voicemail/{ACCOUNT}", 3)}
    result = _list(_Backup(files))
    assert [(v["id"], v["phone_number"], v["date_received"][:19]) for v in result["voicemails"]] == [
        (31, "+15550000013", "2023-11-14T22:13:20"),
    ]
    calls = CallExtractor()._read_voicemails_as_calls(_Backup(files), {})
    assert [c["call_id"] for c in calls] == ["vm_31"]


def test_a_copy_without_audio_beside_it_gives_way_to_the_one_with(tmp_path):
    """The main row's .amr is gone and the line's copy, without its "+", has one: the copy is listed and plays."""
    main = _store(tmp_path / "main" / "voicemail.db", [(31, 5004, 1_700_000_000, "+15550000013", 30, LIVE, 0)])
    account = _store(tmp_path / "acct" / "voicemail.db", [(3, 6004, 1_700_000_000, "15550000013", 30, 0x3, 0)])
    files = {MAIN: main, ACCOUNT_DB: account, **_audio(tmp_path, f"Library/Voicemail/{ACCOUNT}", 3)}
    assert [v["id"] for v in _list(_Backup(files))["voicemails"]] == [f"{ACCOUNT}.3"]


def test_two_seconds_apart_are_two_voicemails(tmp_path):
    main = _store(tmp_path / "main" / "voicemail.db", [(31, 5004, 1_700_000_000, "+15550000013", 30, LIVE, 0)])
    account = _store(tmp_path / "acct" / "voicemail.db", [(3, 6004, 1_700_000_002, "+15550000013", 30, 0x3, 0)])
    assert len(_list(_Backup({MAIN: main, ACCOUNT_DB: account}))["voicemails"]) == 2


def test_a_caller_with_no_number_is_unknown(tmp_path):
    """No caller ID: sender NULL. The list says "Unknown", as Calls does, and has no number for it (#310)."""
    db = _store(tmp_path / "main" / "voicemail.db", [
        (1, 9001, 1_700_000_000, None, 5, LIVE, 0),
        (2, 9002, 1_700_000_100, "", 6, LIVE, 0),
    ])
    result = VoicemailExtractor().list_voicemails(_Backup({MAIN: db}), contacts={"": "Nobody"})
    assert [(v["phone_number"], v["contact_name"]) for v in result["voicemails"]] == [("", "Unknown"), ("", "Unknown")]
