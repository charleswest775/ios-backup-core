"""A note's text comes from its own field in the note data, even when it's only a number.

Picking the longest string in the data skipped strings with fewer than three
letters, so a note that was only a number (a code, a locker combination) came
back with no text.
"""

import gzip
import os
import random
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backupgen.writers.notes import note_data  # noqa: E402
from ios_backup_core.extractors.notes import NoteExtractor, note_text  # noqa: E402

EDITED = datetime(2026, 3, 1, tzinfo=timezone.utc)


def data(*lines):
    return note_data([(line, None, None) for line in lines], EDITED, random.Random(1))


def test_the_text_field_of_the_note():
    assert note_text(gzip.decompress(data("Groceries", "eggs, milk"))) == "Groceries\neggs, milk"
    assert note_text(gzip.decompress(data("4417"))) == "4417"


def test_data_that_isnt_a_note_has_no_text():
    assert note_text(b"\xff\xff\xff") is None
    assert note_text(b"") is None


def test_a_note_that_is_only_numbers_has_its_text(tmp_path):
    path = str(tmp_path / "NoteStore.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER PRIMARY KEY, ZTITLE1 TEXT, ZSNIPPET TEXT,
            ZCREATIONDATE3 REAL, ZMODIFICATIONDATE1 REAL);
        CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZNOTE INTEGER, ZDATA BLOB);
    """)
    for pk, lines in ((1, ("4417",)), (2, ("12", "34")), (3, ("Bike sizes", "54 cm frame"))):
        conn.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (?, ?, NULL, 1.0, ?)", (pk, lines[0], float(pk)))
        conn.execute("INSERT INTO ZICNOTEDATA (ZNOTE, ZDATA) VALUES (?, ?)", (pk, data(*lines)))
    conn.commit()
    conn.close()
    bodies = {n["note_id"]: n["body"] for n in NoteExtractor()._parse_notestore(path)}
    assert bodies == {1: "4417", 2: "12\n34", 3: "Bike sizes\n54 cm frame"}
