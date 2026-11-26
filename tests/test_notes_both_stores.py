"""A backup with notes in both NoteStore.sqlite and the old notes.sqlite.

iOS 9 left each account's notes in notes.sqlite until the account was upgraded,
so a phone restored from backups for years can carry an old "On My iPhone"
account there beside its iCloud notes in NoteStore. list_notes used to read
notes.sqlite first and stop when that had any, so such a backup listed only
its few old notes and none of the rest.
"""

import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.notes import NoteExtractor  # noqa: E402

NOTES_DOMAIN = "AppDomainGroup-group.com.apple.notes"
DAY = 86400.0


class _Backup:
    """Accessor stub: ``files`` maps (domain, relative path) to a local file."""

    def __init__(self, files):
        self.files = files

    def get_file(self, relative_path, domain="HomeDomain"):
        return self.files.get((domain, relative_path))

    def list_files(self, domain=None, path_like=None):
        pattern = ".*".join(re.escape(p) for p in (path_like or "%").split("%"))
        return [{"hash": "", "domain": d, "path": p} for d, p in self.files
                if (domain is None or d == domain) and re.fullmatch(pattern, p, re.IGNORECASE)]


def note_store(path, notes):
    """NoteStore.sqlite with one iCloud folder; ``notes`` is (Z_PK, title, modified)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (
            Z_PK INTEGER PRIMARY KEY, ZTITLE1 TEXT, ZTITLE2 TEXT, ZSNIPPET TEXT, ZFOLDER INTEGER,
            ZCREATIONDATE3 REAL, ZMODIFICATIONDATE1 REAL);
        CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZNOTE INTEGER, ZDATA BLOB);
        INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE2) VALUES (100, 'Notes');
    """)
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE1, ZSNIPPET, ZFOLDER, ZCREATIONDATE3, ZMODIFICATIONDATE1)"
        " VALUES (?, ?, ?, 100, ?, ?)",
        [(pk, title, f"{title} body", modified, modified) for pk, title, modified in notes])
    conn.commit()
    conn.close()
    return str(path)


def legacy_store(path, notes):
    """notes.sqlite as iOS 4 to 8 wrote it; ``notes`` is (Z_PK, title, modified)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ZNOTE (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZCREATIONDATE REAL,
            ZMODIFICATIONDATE REAL, ZDELETEDFLAG INTEGER);
        CREATE TABLE ZNOTEBODY (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZCONTENT TEXT);
    """)
    for pk, title, modified in notes:
        conn.execute("INSERT INTO ZNOTE VALUES (?, ?, ?, ?, 0)", (pk, title, modified, modified))
        conn.execute("INSERT INTO ZNOTEBODY (ZOWNER, ZCONTENT) VALUES (?, ?)", (pk, f"<div>{title} body</div>"))
    conn.commit()
    conn.close()
    return str(path)


def test_notes_in_both_files_are_all_listed_newest_first(tmp_path):
    backup = _Backup({
        (NOTES_DOMAIN, "NoteStore.sqlite"): note_store(tmp_path / "NoteStore.sqlite", [
            (6, "Packing list", 700 * DAY), (8, "Gift ideas", 300 * DAY), (12, "Recipes", 900 * DAY)]),
        ("HomeDomain", "Library/Notes/notes.sqlite"): legacy_store(tmp_path / "notes.sqlite", [
            (6, "Old phone numbers", 400 * DAY), (9, "Bike sizes", 100 * DAY)]),
    })
    notes = NoteExtractor().list_notes(backup)["notes"]
    assert [n["title"] for n in notes] == ["Recipes", "Packing list", "Old phone numbers", "Gift ideas", "Bike sizes"]
    assert [n["note_id"] for n in notes] == [12, 6, "legacy_6", 8, "legacy_9"]
    assert {n["title"]: n["body"] for n in notes}["Old phone numbers"] == "Old phone numbers body"


def test_only_notes_sqlite_keeps_its_ids(tmp_path):
    backup = _Backup({("HomeDomain", "Library/Notes/notes.sqlite"): legacy_store(
        tmp_path / "notes.sqlite", [(3, "Groceries", 2 * DAY), (4, "Passwords hint", 5 * DAY)])})
    notes = NoteExtractor().list_notes(backup)["notes"]
    assert [(n["note_id"], n["title"]) for n in notes] == [(4, "Passwords hint"), (3, "Groceries")]


def test_an_empty_notes_sqlite_beside_notestore(tmp_path):
    backup = _Backup({
        (NOTES_DOMAIN, "NoteStore.sqlite"): note_store(tmp_path / "NoteStore.sqlite", [(5, "Recipes", DAY)]),
        ("HomeDomain", "Library/Notes/notes.sqlite"): legacy_store(tmp_path / "notes.sqlite", []),
    })
    assert [(n["note_id"], n["title"]) for n in NoteExtractor().list_notes(backup)["notes"]] == [(5, "Recipes")]


def test_notestore_found_where_ios_keeps_it_when_the_file_list_cant_be_searched(tmp_path):
    class Unlisted(_Backup):
        def list_files(self, domain=None, path_like=None):
            return []

    backup = Unlisted({
        (NOTES_DOMAIN, "NoteStore.sqlite"): note_store(tmp_path / "NoteStore.sqlite", [(5, "Recipes", DAY)]),
        ("HomeDomain", "Library/Notes/notes.sqlite"): legacy_store(tmp_path / "notes.sqlite", [(5, "Old", 2 * DAY)]),
    })
    notes = NoteExtractor().list_notes(backup)["notes"]
    assert [(n["note_id"], n["title"]) for n in notes] == [("legacy_5", "Old"), (5, "Recipes")]
