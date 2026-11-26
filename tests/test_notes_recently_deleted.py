"""Notes in the iPhone's Recently Deleted folder are listed with is_deleted.

The folder is a row with ZFOLDERTYPE 1, and a note in it can also have
ZMARKEDFORDELETION set: both were in an iOS 27.3 backup. Either one marks the note.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.notes import NoteExtractor  # noqa: E402


def note_store(path, columns, rows):
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER PRIMARY KEY, {', '.join(columns)})")
    conn.execute("CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZNOTE INTEGER, ZDATA BLOB)")
    for row in rows:
        conn.execute(f"INSERT INTO ZICCLOUDSYNCINGOBJECT ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))
    conn.commit()
    conn.close()
    return str(path)


COLUMNS = ["ZTITLE1 TEXT", "ZTITLE2 TEXT", "ZSNIPPET TEXT", "ZFOLDER INTEGER", "ZFOLDERTYPE INTEGER",
           "ZMARKEDFORDELETION INTEGER", "ZCREATIONDATE1 REAL", "ZMODIFICATIONDATE1 REAL"]


def test_notes_in_recently_deleted_or_marked_for_deletion(tmp_path):
    path = note_store(tmp_path / "NoteStore.sqlite", COLUMNS, [
        {"Z_PK": 1, "ZTITLE2": "Notes", "ZFOLDERTYPE": 0, "ZMARKEDFORDELETION": 0},
        {"Z_PK": 2, "ZTITLE2": "Recently Deleted", "ZFOLDERTYPE": 1, "ZMARKEDFORDELETION": 0},
        {"Z_PK": 3, "ZTITLE2": "Smart", "ZFOLDERTYPE": 3, "ZMARKEDFORDELETION": 0},
        {"Z_PK": 10, "ZTITLE1": "Kept", "ZFOLDER": 1, "ZMARKEDFORDELETION": 0, "ZMODIFICATIONDATE1": 6.0},
        {"Z_PK": 11, "ZTITLE1": "Both", "ZFOLDER": 2, "ZMARKEDFORDELETION": 1, "ZMODIFICATIONDATE1": 5.0},
        {"Z_PK": 12, "ZTITLE1": "In the folder", "ZFOLDER": 2, "ZMARKEDFORDELETION": 0, "ZMODIFICATIONDATE1": 4.0},
        {"Z_PK": 13, "ZTITLE1": "Only marked", "ZFOLDER": 1, "ZMARKEDFORDELETION": 1, "ZMODIFICATIONDATE1": 3.0},
        {"Z_PK": 14, "ZTITLE1": "No folder", "ZMODIFICATIONDATE1": 2.0},
        {"Z_PK": 15, "ZTITLE1": "Smart folder", "ZFOLDER": 3, "ZMARKEDFORDELETION": 0, "ZMODIFICATIONDATE1": 1.0},
    ])
    notes = NoteExtractor()._parse_notestore(path)
    # Nothing is left out: deleted notes are listed in date order with the others
    assert [(n["title"], n["is_deleted"]) for n in notes] == [
        ("Kept", False), ("Both", True), ("In the folder", True), ("Only marked", True),
        ("No folder", False), ("Smart folder", False),
    ]


def test_a_store_without_the_columns_has_no_deleted_notes(tmp_path):
    path = note_store(tmp_path / "old.sqlite", ["ZTITLE1 TEXT", "ZSNIPPET TEXT", "ZFOLDER INTEGER",
                                                "ZCREATIONDATE1 REAL", "ZMODIFICATIONDATE1 REAL"], [
        {"Z_PK": 1, "ZTITLE1": "Old note", "ZFOLDER": 2, "ZMODIFICATIONDATE1": 1.0},
    ])
    [note] = NoteExtractor()._parse_notestore(path)
    assert (note["title"], note["is_deleted"]) == ("Old note", False)


def test_legacy_notes_say_they_are_not_deleted(tmp_path):
    # notes.sqlite (iOS 4 to 8) leaves its deleted notes out, so the ones listed are kept
    conn = sqlite3.connect(tmp_path / "notes.sqlite")
    conn.executescript("""
        CREATE TABLE ZNOTE (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZCREATIONDATE REAL,
                            ZMODIFICATIONDATE REAL, ZDELETEDFLAG INTEGER);
        CREATE TABLE ZNOTEBODY (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZCONTENT TEXT);
        INSERT INTO ZNOTE VALUES (1, 'Kept', 1.0, 1.0, 0), (2, 'Gone', 1.0, 1.0, 1);
        INSERT INTO ZNOTEBODY VALUES (1, 1, '<div>Kept</div>');
    """)
    conn.commit()
    conn.close()
    notes = NoteExtractor()._parse_legacy_notes(str(tmp_path / "notes.sqlite"))
    assert [(n["title"], n["is_deleted"]) for n in notes] == [("Kept", False)]
