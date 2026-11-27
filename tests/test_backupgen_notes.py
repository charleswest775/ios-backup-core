"""The test persona's Notes have the shapes a Notes reader must handle, and list_notes reads them all.

- An old notes.sqlite beside NoteStore, with an "On My iPhone" account iOS 9 never
  upgraded (writers/legacy_notes.py): its notes are listed with NoteStore's, as
  "legacy_<Z_PK>", and one with ZDELETEDFLAG 1 isn't (another is listed until it's
  deleted after the iOS 15 backup, and one is edited after it). list_notes used to stop
  at notes.sqlite when it had notes, so only those few were listed (ios-backup-core#40).
- Notes with no letters: a code, two numbers on two lines, emoji. Picking the longest
  string with three letters or more gave them no text (also #40).
- A note that's only its title has a NULL snippet (and one has '').
- Notes whose text can't be read: ICNoteData.ZDATA NULL or empty, or no row at all.
- A placeholder for a note not yet downloaded from iCloud: no title, never listed.
- Notes in Recently Deleted are listed with is_deleted (#41).

Einstein never used his phone's Notes app: by iOS 26 iCloud has set up a NoteStore with
an account, an empty "Notes" folder and a placeholder for a note still to download, so
Notes is there with nothing to show; his iOS 15 and 17 backups have no NoteStore at all.
"""

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen import PERSONAS_DIR  # noqa: E402
from backupgen.generate import EXPECTED, generate  # noqa: E402
from backupgen.persona import PersonaError, load_persona  # noqa: E402
from backupgen.writers.notes import note_account_column  # noqa: E402
from backupgen.sqlitegen import load_schema  # noqa: E402
from ios_backup_core.backup import open_local_backup  # noqa: E402
from ios_backup_core.extractors.notes import NoteExtractor  # noqa: E402

NOTES_DOMAIN = "AppDomainGroup-group.com.apple.notes"
LEGACY = ("HomeDomain", "Library/Notes/notes.sqlite")
SNAPSHOTS = [("ios15", None), ("ios17", "pw"), ("ios26", None)]
LETTERLESS = {"4417": "4417", "12": "12\n34", "🎂🎉": "🎂🎉\n🎈🎈🎈", "Call the dentist": "Call the dentist"}
UNREADABLE = {"Wi-Fi at the cabin": "null", "Parking level": "empty", "Recipe from Grandma": "missing"}
# The legacy notes listed in each snapshot, with their ids: "Spare key" is deleted between iOS 15 and 17.
LEGACY_LISTED = {
    "ios15": {"Gift ideas": "legacy_2", "Bike sizes": "legacy_3", "Spare key": "legacy_4"},
    "ios17": {"Gift ideas": "legacy_2", "Bike sizes": "legacy_3"},
    "ios26": {"Gift ideas": "legacy_2", "Bike sizes": "legacy_3"},
}
LEGACY_DELETED = {"ios15": ["Old shopping list"], "ios17": ["Old shopping list", "Spare key"],
                  "ios26": ["Old shopping list", "Spare key"]}


class Backup:
    """A generated snapshot: its answer key, list_notes, and its Notes databases' rows."""

    def __init__(self, folder: Path, password=None, persona="testpersona", snapshot="ios26", root=None):
        pack = load_persona(persona, root=root) if root else None
        backup_dir = generate(persona, snapshot, folder, password=password, persona=pack)
        self.expected = json.loads((folder / EXPECTED).read_text(encoding="utf-8"))
        accessor = open_local_backup(str(backup_dir), password=password)
        try:
            self.notes = NoteExtractor().list_notes(accessor)["notes"]
            self.note_store = self._rows(accessor.get_file("NoteStore.sqlite", NOTES_DOMAIN), "ZICCLOUDSYNCINGOBJECT")
            self.note_data = self._rows(accessor.get_file("NoteStore.sqlite", NOTES_DOMAIN), "ZICNOTEDATA")
            self.legacy = self._rows(accessor.get_file(LEGACY[1], LEGACY[0]), "ZNOTE")
            self.schema = load_schema(self.expected["ios"], "NoteStore.sqlite")
        finally:
            accessor.cleanup()

    @staticmethod
    def _rows(path, table):
        if not path or not os.path.isfile(path):
            return None
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY Z_PK")]
        finally:
            conn.close()

    def note_rows(self):
        entity = {r["Z_NAME"]: r["Z_ENT"] for r in self.schema["entities"]}["ICNote"]
        return [r for r in self.note_store if r["Z_ENT"] == entity]


@pytest.fixture(scope="module", params=SNAPSHOTS, ids=[s for s, _ in SNAPSHOTS])
def snapshot(request, tmp_path_factory):
    name, password = request.param
    return Backup(tmp_path_factory.mktemp(name), password, snapshot=name)


def _by_id(notes, text):
    """note_id (as given: 12 and "12" differ) → what's compared; the ids must be unique."""
    by_id = {n["note_id"]: (n["title"], n[text], n["created"], n["modified"],
                            n["is_deleted" if text == "body" else "deleted"], n["shared_with"]) for n in notes}
    assert len(by_id) == len(notes), "two notes share an id"
    return by_id


def test_list_notes_is_the_answer_key_legacy_notes_and_ids_included(snapshot):
    assert _by_id(snapshot.notes, "body") == _by_id(snapshot.expected["notes"], "text")
    legacy = [n for n in snapshot.expected["notes"] if n["store"] == "legacy"]
    # NoteStore has notes too, so the reader puts "legacy_" in front of notes.sqlite's row numbers
    assert {n["title"]: n["note_id"] for n in legacy} == LEGACY_LISTED[snapshot.expected["snapshot"]]
    assert {n["store"] for n in snapshot.expected["notes"]} == {"notestore", "legacy"}
    assert all(type(n["note_id"]) is int for n in snapshot.expected["notes"] if n["store"] == "notestore")
    # Bike sizes was edited after the iOS 15 backup, which still has it as it was written
    bike = next(n for n in legacy if n["title"] == "Bike sizes")
    assert bike["modified"] == ("2021-01-08T08:15:00+00:00" if snapshot.expected["snapshot"] == "ios15"
                                else "2023-06-01T17:20:00+00:00")


def test_notes_with_no_letters_come_back_exactly(snapshot):
    bodies = {n["title"]: n["body"] for n in snapshot.notes}
    assert {title: bodies[title] for title in LETTERLESS} == LETTERLESS
    assert {n["title"]: n["body"] for n in snapshot.notes if n["title"] in ("Gift ideas", "Bike sizes")} == {
        "Gift ideas": "Gift ideas\nRiley: a good pen\n\nJordan: maps & <old> guides", "Bike sizes": "Bike sizes\n54\n72.5"}


def test_a_title_alone_has_a_null_snippet(snapshot):
    snippets = {r["ZTITLE1"]: r["ZSNIPPET"] for r in snapshot.note_rows() if r["ZTITLE1"]}
    assert snippets["4417"] is None and snippets["12"] == "34" and snippets["🎂🎉"] == "🎈🎈🎈"
    assert snippets["Call the dentist"] == ""  # the one '' (snippet: empty)
    assert all(snippets[title] is None for title in UNREADABLE)


def test_notes_whose_text_cant_be_read_have_their_title_and_no_text(snapshot):
    rows = {r["ZTITLE1"]: r for r in snapshot.note_rows() if r["ZTITLE1"]}
    data = {r["ZNOTE"]: r["ZDATA"] for r in snapshot.note_data}
    shapes = {}
    for title in UNREADABLE:
        row = rows[title]
        if row["ZNOTEDATA"] is None:
            assert row["Z_PK"] not in data
            shapes[title] = "missing"
        else:
            shapes[title] = "null" if data[row["Z_PK"]] is None else "empty" if data[row["Z_PK"]] == b"" else "text"
    assert shapes == UNREADABLE
    listed = {n["title"]: n for n in snapshot.notes}
    assert {title: listed[title]["body"] for title in UNREADABLE} == dict.fromkeys(UNREADABLE, "")
    assert all(listed[title]["created"] for title in UNREADABLE)


def test_the_placeholder_and_the_deleted_legacy_note_arent_listed(snapshot):
    [placeholder] = [r for r in snapshot.note_rows() if r["ZTITLE1"] is None]
    account = note_account_column(snapshot.schema)
    assert placeholder["ZNEEDSINITIALFETCHFROMCLOUD"] == 1 and placeholder["ZIDENTIFIER"]
    assert placeholder[account] == 1 and placeholder["ZFOLDER"] is None and placeholder["ZMODIFICATIONDATE1"] is None
    assert {r["ZNOTE"]: r["ZDATA"] for r in snapshot.note_data}[placeholder["Z_PK"]] is None
    assert placeholder["Z_PK"] not in {n["note_id"] for n in snapshot.notes}
    deleted = [r["ZTITLE"] for r in snapshot.legacy if r["ZDELETEDFLAG"] == 1]
    assert deleted == LEGACY_DELETED[snapshot.expected["snapshot"]]
    assert not set(deleted) & ({n["title"] for n in snapshot.notes} | {n["title"] for n in snapshot.expected["notes"]})
    assert len(snapshot.notes) == len(snapshot.note_rows()) - 1 + len(snapshot.legacy) - len(deleted)


def test_notes_point_at_their_account_and_say_if_they_have_a_checklist(snapshot):
    column = note_account_column(snapshot.schema)
    assert column == {"ios15": "ZACCOUNT4", "ios17": "ZACCOUNT7", "ios26": "ZACCOUNT7"}[snapshot.expected["snapshot"]]
    entities = {r["Z_NAME"]: r["Z_ENT"] for r in snapshot.schema["entities"]}
    [account] = [r for r in snapshot.note_store if r["Z_ENT"] == entities["ICAccount"]]
    assert {r[column] for r in snapshot.note_rows()} == {account["Z_PK"]}
    assert {r["ZISPASSWORDPROTECTED"] for r in snapshot.note_rows() if r["ZTITLE1"]} == {0}
    [data] = [r for r in snapshot.note_store if r["Z_ENT"] == entities["ICAccountData"]]
    assert account["ZACCOUNTDATA"] == data["Z_PK"] and data["ZACCOUNT"] == account["Z_PK"]
    folders = {r["ZTITLE2"] for r in snapshot.note_store if r["Z_ENT"] == entities["ICFolder"]}
    assert "Notes" in folders
    if "ZHASCHECKLIST" in snapshot.note_rows()[0]:  # iOS 16 and later
        flags = {r["ZTITLE1"]: (r["ZHASCHECKLIST"], r["ZHASCHECKLISTINPROGRESS"]) for r in snapshot.note_rows() if r["ZTITLE1"]}
        assert flags["Release checklist"] == (1, 1) and flags["Groceries"] == (0, 0)


def test_note_account_column_in_each_harvested_schema():
    assert {version: note_account_column(load_schema(version, "NoteStore.sqlite"))
            for version in ("13.3.1", "13.4.1", "15.3.1", "16.1.2", "17.3")} == {
        "13.3.1": "ZACCOUNT3", "13.4.1": "ZACCOUNT3", "15.3.1": "ZACCOUNT4", "16.1.2": "ZACCOUNT7", "17.3": "ZACCOUNT7"}


# ── Einstein: Notes there, nothing to show; and a backup with no NoteStore at all ──

@pytest.mark.parametrize("name, password", [("ios15", None), ("ios17", "pw"), ("ios26", None)])
def test_einstein_never_used_notes(tmp_path, name, password):
    backup = Backup(tmp_path, password, persona="einstein", snapshot=name)
    assert backup.notes == [] and backup.expected["notes"] == [] and backup.expected["absent"]["notes"] == "none"
    assert backup.legacy is None
    if name != "ios26":
        assert backup.note_store is None  # "not in this backup"
        return
    entities = {r["Z_ENT"]: r["Z_NAME"] for r in backup.schema["entities"]}
    assert sorted(entities[r["Z_ENT"]] for r in backup.note_store) == ["ICAccount", "ICAccountData", "ICFolder", "ICNote"]
    [folder] = [r for r in backup.note_store if entities[r["Z_ENT"]] == "ICFolder"]
    assert folder["ZTITLE2"] == "Notes" and folder["ZFOLDERTYPE"] == 0
    [note] = backup.note_rows()
    assert note["ZTITLE1"] is None and note["ZNEEDSINITIALFETCHFROMCLOUD"] == 1


# ── Legacy notes alone, and the part_of step ─────────────────────────────────────

def _pack(tmp_path: Path, keep, options=None) -> Path:
    """The test persona with only the notes files ``keep`` lets through (and these Notes options)."""
    shutil.copytree(PERSONAS_DIR / "testpersona", tmp_path / "testpersona")
    for path in (tmp_path / "testpersona" / "notes").glob("*.txt"):
        if not keep(path.name):
            path.unlink()
    persona_json = tmp_path / "testpersona" / "persona.json"
    data = json.loads(persona_json.read_text(encoding="utf-8"))
    data["options"] = {"notes": options} if options is not None else {}
    persona_json.write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


def test_a_phone_with_only_legacy_notes_keeps_their_row_numbers(tmp_path):
    # NoteStore has a placeholder but lists nothing, so the legacy notes keep their plain row numbers
    root = _pack(tmp_path, lambda name: name.startswith("legacy-"), {"placeholders": 1})
    backup = Backup(tmp_path / "out", root=root)
    assert [r["ZTITLE1"] for r in backup.note_rows()] == [None]
    assert [(n["note_id"], n["title"]) for n in backup.expected["notes"]] == [(2, "Gift ideas"), (3, "Bike sizes")]
    assert sorted((n["note_id"], n["title"]) for n in backup.notes) == [(2, "Gift ideas"), (3, "Bike sizes")]
    assert "notes" not in backup.expected["absent"]  # what legacy_notes writes makes Notes present


def test_only_a_deleted_legacy_note_is_no_notes(tmp_path):
    root = _pack(tmp_path, lambda name: name == "legacy-old-list.txt")
    backup = Backup(tmp_path / "out", root=root)
    assert backup.legacy and [r["ZDELETEDFLAG"] for r in backup.legacy] == [1]
    assert backup.notes == [] and backup.expected["notes"] == [] and backup.expected["absent"]["notes"] == "none"


@pytest.mark.parametrize("text, message", [
    ("store: legacy\nfolder: Work\ncreated: 2021-02-01 10:00\n\nOld\n", "no folder:"),
    ("store: legacy\nshared: riley\ncreated: 2021-02-01 10:00\n\nOld\n", "no shared:"),
    ("store: legacy\ncreated: 2021-02-01 10:00\n\nOld\ntwo  spaces\n", "line 2 of a store: legacy note"),
    ("store: legacy\ncreated: 2021-02-01 10:00\n\nOld\n\ttab\n", "line 2 of a store: legacy note"),
    ("store: legacy\ncreated: 2021-02-01 10:00\n\nOld\n\n\nfar\n", "two blank lines in a row"),
    ("store: cloud\ncreated: 2021-02-01 10:00\n\nOld\n", "store: is one of notestore, legacy"),
    ("data: gone\ncreated: 2021-02-01 10:00\n\nTitle\n", "data: is one of null, empty, missing"),
    ("data: null\ncreated: 2021-02-01 10:00\n\nTitle\nmore\n", "a note with data: is only its title"),
    ("snippet: none\ncreated: 2021-02-01 10:00\n\nTitle\n", "snippet: can only be empty"),
    ("snippet: empty\ncreated: 2021-02-01 10:00\n\nTitle\nmore\n", "a note with snippet: is only its title"),
])
def test_note_headers_that_cant_be_written_are_refused(tmp_path, text, message):
    root = _pack(tmp_path, lambda name: False)
    (root / "testpersona" / "notes" / "bad.txt").write_text(text, encoding="utf-8")
    with pytest.raises(PersonaError, match=message):
        load_persona("testpersona", root=root)


@pytest.mark.parametrize("value", [-1, "1", 1.5, True])
def test_placeholders_is_a_count(tmp_path, value):
    root = _pack(tmp_path, lambda name: True, {"placeholders": value})
    with pytest.raises(PersonaError, match="options.notes.placeholders"):
        generate("testpersona", "ios26", tmp_path / "out", persona=load_persona("testpersona", root=root))


def test_placeholders_alone_make_a_note_store_with_nothing_to_list(tmp_path):
    root = _pack(tmp_path, lambda name: True, {"placeholders": 2})
    shutil.rmtree(root / "testpersona" / "notes")
    backup = Backup(tmp_path / "out", root=root)
    assert [r["ZTITLE1"] for r in backup.note_rows()] == [None, None] and backup.legacy is None
    assert backup.notes == [] and backup.expected["absent"]["notes"] == "none"
