"""backupgen's voicemail in the shapes a reader must handle, read back by the reader.

The test persona's voicemails.txt has one of each (persona/voicemail.py): the Spam and
Deleted lists (OE-79 listed spam as voicemail), deleted ones with and without a
trashed_date, voicemail.db's deleted table and audio with no row (which aren't listed),
a caller with no number (#310), one never transcribed, and the per-line store
A-<UUID>/voicemail.db, whose copies of main rows have other ROWIDs and remote_uids, a
number without its "+" or a date a second off, the only audio of one voicemail, the only
record of one deletion, and one voicemail of its own (OE-85 missed one that was only there).
The answer key says what a reader should list, so these check the reader against it.
"""

import json
import os
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen import PERSONAS_DIR  # noqa: E402
from backupgen.generate import EXPECTED, generate  # noqa: E402
from backupgen.persona import PersonaError, load_persona  # noqa: E402
from backupgen.writers import COCOA_EPOCH  # noqa: E402
from backupgen.writers import voicemail as voicemail_writer  # noqa: E402
from ios_backup_core.backup import open_local_backup  # noqa: E402
from ios_backup_core.contacts import ContactResolver  # noqa: E402
from ios_backup_core.extractors.calls import CallExtractor  # noqa: E402
from ios_backup_core.extractors.voicemail import VoicemailExtractor  # noqa: E402

PASSWORD = "pw"
MAIN = "Library/Voicemail/voicemail.db"
SNAPSHOTS = [("ios15", None), ("ios17", PASSWORD), ("ios26", None), ("ios26", PASSWORD)]
IDS = [f"{s}-{'encrypted' if p else 'plain'}" for s, p in SNAPSHOTS]


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    """(snapshot, password) -> (answer key, open backup accessor), each made once."""
    root = tmp_path_factory.mktemp("voicemail")
    out, opened = {}, []
    for snapshot, password in SNAPSHOTS:
        folder = root / f"{snapshot}-{bool(password)}"
        backup_dir = generate("testpersona", snapshot, folder, password=password)
        accessor = open_local_backup(str(backup_dir), password=password)
        opened.append(accessor)
        out[(snapshot, password)] = (json.loads((folder / EXPECTED).read_text(encoding="utf-8")), accessor)
    yield out
    for accessor in opened:
        accessor.cleanup()


@pytest.fixture(params=SNAPSHOTS, ids=IDS)
def backup(request, made):
    return made[request.param]


def _rows(accessor, relative_path: str, sql: str) -> list:
    path = accessor.get_file(relative_path, domain="HomeDomain")
    assert path, relative_path
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _paths(accessor) -> set:
    return {e["path"] for e in accessor.list_files(domain="HomeDomain", path_like="Library/Voicemail/%")}


def _line_store(accessor):
    """(folder name, relative path) of the per-line store, or None."""
    stores = [p for p in _paths(accessor) if p.startswith("Library/Voicemail/A-") and p.endswith("/voicemail.db")]
    assert len(stores) <= 1
    return (stores[0].split("/")[2], stores[0]) if stores else None


def _listed(v: dict) -> tuple:
    return (str(v["id"]), v["phone_number"], v["contact_name"], v["date_received"], v["duration"], v["transcript"],
            v["folder"], v["is_spam"], v["is_deleted"], v["is_read"])


def _wanted(v: dict) -> tuple:
    return (str(v["id"]), v["sender"] or "", v["display_name"], v["date"], v["duration"], v["transcript"],
            v["folder"], v["is_spam"], v["is_deleted"], v["is_read"])


# ── the list ─────────────────────────────────────────────────────────────────────


def test_every_list_matches_the_answer_key(backup):
    expected, accessor = backup
    contacts = ContactResolver().load_contacts(accessor)
    result = VoicemailExtractor().list_voicemails(accessor, contacts, include_deleted=True)
    assert "error" not in result, result.get("error")
    assert [_listed(v) for v in result["voicemails"]] == sorted(
        (_wanted(v) for v in expected["voicemails"]), key=lambda t: t[3], reverse=True)
    assert result["counts"] == {folder: sum(1 for v in expected["voicemails"] if v["folder"] == folder)
                                for folder in ("voicemail", "spam", "deleted")}


def test_the_default_list_leaves_out_deleted_ones(backup):
    expected, accessor = backup
    contacts = ContactResolver().load_contacts(accessor)
    listed = VoicemailExtractor().list_voicemails(accessor, contacts)["voicemails"]
    assert Counter(_listed(v) for v in listed) == Counter(_wanted(v) for v in expected["voicemails"]
                                                          if not v["is_deleted"])


def test_the_answer_key_has_each_shape(made):
    """So the checks above check something: each case is in the iOS 26 snapshot, and the iOS 15 one, older, has fewer."""
    key = made[("ios26", None)][0]
    voicemails, removed = key["voicemails"], key["removed_voicemails"]
    assert Counter(v["folder"] for v in voicemails) == {"voicemail": 10, "spam": 2, "deleted": 3}
    assert sum(1 for v in voicemails if v["is_spam"] and not v["is_read"]) == 1
    assert [v["display_name"] for v in voicemails if v["sender"] is None] == ["Unknown"]
    assert sum(1 for v in voicemails if not v["transcript"]) == 1
    assert [v["sender"] for v in voicemails if not v["audio_available"]] == ["+447700900099"]
    assert sum(1 for v in voicemails if str(v["id"]).startswith("A-")) == 2  # line-audio and line-only
    assert sorted((r["table"] or "", r["audio_file"] is not None) for r in removed) == [
        ("", True), ("deleted", False), ("deleted", False), ("deleted", True), ("deleted", True)]
    old = made[("ios15", None)][0]
    assert not any(v["folder"] == "spam" or str(v["id"]).startswith("A-") for v in old["voicemails"])


def test_audio_is_where_the_answer_key_says(backup):
    expected, accessor = backup
    for v in expected["voicemails"]:
        path = VoicemailExtractor().get_audio_path(accessor, v["id"])
        assert (path is not None) == v["audio_available"], v["id"]
        if path:
            audio = Path(path).read_bytes()
            assert audio.startswith(b"#!AMR\n") and len(audio) == 6 + 32 * 50 * v["duration"], v["id"]
    assert any(not v["audio_available"] for v in expected["voicemails"]), "a listed voicemail without its audio"


def test_a_voicemail_only_in_the_line_store_plays(made):
    expected, accessor = made[("ios26", PASSWORD)]
    line, _ = _line_store(accessor)
    for v in expected["voicemails"]:
        if str(v["id"]).startswith("A-"):
            store, rowid = str(v["id"]).split(".")
            assert store == line
            assert VoicemailExtractor().get_audio_path(accessor, v["id"]) == accessor.get_file(
                f"Library/Voicemail/{line}/{rowid}.amr", domain="HomeDomain")


def test_voicemails_not_in_any_list_arent_listed(backup):
    expected, accessor = backup
    contacts = ContactResolver().load_contacts(accessor)
    listed = {(v["date_received"], v["duration"], v["phone_number"])
              for v in VoicemailExtractor().list_voicemails(accessor, contacts, include_deleted=True)["voicemails"]}
    for r in expected["removed_voicemails"]:
        assert (r["date"], r["duration"], r["sender"] or "") not in listed, r


def test_removed_voicemails_are_rows_of_the_deleted_table(backup):
    expected, accessor = backup
    main = {r["ROWID"] for r in _rows(accessor, MAIN, "SELECT ROWID FROM voicemail")}
    deleted = {r["ROWID"]: r for r in _rows(accessor, MAIN, "SELECT * FROM deleted")}
    assert main.isdisjoint(deleted), "a removed voicemail's ROWID is a listed one's, and so would its .amr be"
    paths = _paths(accessor)
    for r in expected["removed_voicemails"]:
        assert (r["rowid"] in deleted) == (r["table"] == "deleted"), r
        assert r["rowid"] not in main
        if r["audio_file"]:
            assert r["audio_file"] == f"Library/Voicemail/{r['rowid']}.amr" and r["audio_file"] in paths
        else:
            assert f"Library/Voicemail/{r['rowid']}.amr" not in paths
    # Every one has 0x8; testpersona's removed ones mostly have no trashed_date
    assert all(row["flags"] & 0x8 for row in deleted.values())
    if deleted:
        assert sum(1 for row in deleted.values() if not row["trashed_date"]) > len(deleted) / 2


# ── what the rows say ────────────────────────────────────────────────────────────


def test_flags_are_only_the_known_bits(backup):
    expected, accessor = backup
    rows = {r["ROWID"]: r for r in _rows(accessor, MAIN, "SELECT * FROM voicemail")}
    rows.update({r["ROWID"]: r for r in _rows(accessor, MAIN, "SELECT * FROM deleted")})
    for row in rows.values():
        assert row["flags"] & 0x2 and not row["flags"] & ~0x8B, hex(row["flags"])  # 0x1 0x2 0x8 0x80 only
    for v in expected["voicemails"]:
        if v["rowid"] is None:
            continue
        flags = rows[v["rowid"]]["flags"]
        assert bool(flags & 0x1) == v["is_read"] and bool(flags & 0x80) == v["is_spam"]


def test_deleted_ones_have_a_trashed_date_in_cocoa_seconds_or_none(backup):
    expected, accessor = backup
    rows = {r["ROWID"]: r for r in _rows(accessor, MAIN, "SELECT * FROM voicemail")}
    kinds = Counter()
    for v in expected["voicemails"]:
        row = rows.get(v["rowid"])
        if row is None or not row["flags"] & 0x8:
            continue
        if row["trashed_date"]:
            kinds["trashed"] += 1
            # trashed_date counts from 2001, date from 1970: deleted within two days of coming in
            assert 0 < row["trashed_date"] + COCOA_EPOCH - row["date"] < 2 * 86400
        else:
            kinds["long ago"] += 1
    assert kinds == {"trashed": 1, "long ago": 1}


def test_a_caller_with_no_number(backup):
    expected, accessor = backup
    contacts = ContactResolver().load_contacts(accessor)
    (unknown,) = [v for v in expected["voicemails"] if v["sender"] is None]
    (row,) = _rows(accessor, MAIN, f"SELECT sender, callback_num FROM voicemail WHERE ROWID = {unknown['rowid']}")
    assert (row["sender"], row["callback_num"]) == (None, None)
    (listed,) = [v for v in VoicemailExtractor().list_voicemails(accessor, contacts)["voicemails"]
                 if v["id"] == unknown["id"]]
    assert (listed["phone_number"], listed["contact_name"]) == ("", "Unknown")
    # Its missed call has no number either, and Calls names it the same way; it's one call either way.
    calls = [c for c in CallExtractor().list_calls(accessor, contacts, 0, 1000)["calls"] if not c["address"]]
    assert [(c["contact_name"], c["status"]) for c in calls] == [("Unknown", "missed")]
    if accessor.encrypted:
        assert [c for c in expected["calls"] if c["address"] is None] != []


def test_a_voicemail_never_transcribed_has_no_transcript_file(backup):
    expected, accessor = backup
    transcripts = {p for p in _paths(accessor) if p.endswith(".transcript")}
    line = _line_store(accessor)
    for v in expected["voicemails"]:
        store, _, rowid = str(v["id"]).rpartition(".")
        beside = f"Library/Voicemail/{store}/{rowid}.transcript" if store else f"Library/Voicemail/{rowid}.transcript"
        assert (beside in transcripts) == bool(v["transcript"]), v["id"]
    assert len(transcripts) == sum(1 for v in expected["voicemails"] if v["transcript"])
    assert line is None or any(p.startswith(f"Library/Voicemail/{line[0]}/") for p in transcripts)


# ── the per-line store ───────────────────────────────────────────────────────────


def test_the_line_store_has_the_smaller_layout(made):
    _, accessor = made[("ios26", None)]
    _, relative = _line_store(accessor)
    tables = {r["name"] for r in _rows(accessor, relative, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"voicemail", "_SqliteDatabaseProperties"} <= tables and not tables & {"deleted", "map"}
    columns = {r["name"] for r in _rows(accessor, relative, "PRAGMA table_info(voicemail)")}
    assert "uuid" not in columns and "label" in columns
    main_columns = {r["name"] for r in _rows(accessor, MAIN, "PRAGMA table_info(voicemail)")}
    assert columns == main_columns - {"uuid"}


def test_line_copies_differ_from_their_main_rows(made):
    expected, accessor = made[("ios26", PASSWORD)]
    line, relative = _line_store(accessor)
    copies = _rows(accessor, relative, "SELECT * FROM voicemail ORDER BY ROWID")
    main = _rows(accessor, MAIN, "SELECT * FROM voicemail")
    assert min(r["ROWID"] for r in copies) > 1
    assert {r["remote_uid"] for r in copies}.isdisjoint(r["remote_uid"] for r in main)
    # The main store's map row names the line, and its rows the line holds carry that row's label
    (mapped,) = _rows(accessor, MAIN, "SELECT account, label FROM map")
    assert "A-" + mapped["account"] == line
    labelled = [r for r in main if r["label"] is not None]
    assert labelled and {r["label"] for r in labelled} == {mapped["label"]}

    def twin(copy):
        digits = "".join(ch for ch in copy["sender"] if ch.isdigit())
        return [r for r in main if r["duration"] == copy["duration"] and abs(r["date"] - copy["date"]) <= 1
                and "".join(ch for ch in (r["sender"] or "") if ch.isdigit()) == digits]

    twins = {copy["ROWID"]: twin(copy) for copy in copies}
    assert sorted(len(t) for t in twins.values()) == [0, 1, 1, 1, 1, 1, 1]  # line-only has none
    assert {r["ROWID"] for t in twins.values() for r in t} == {r["ROWID"] for r in labelled}
    pairs = [(copy, t[0]) for copy in copies for t in [twins[copy["ROWID"]]] if t]
    assert all(copy["ROWID"] != row["ROWID"] for copy, row in pairs)  # other ROWIDs than their main rows'
    assert sum(1 for copy, row in pairs if copy["sender"] == row["sender"][1:]) == 1  # no "+"
    assert sum(1 for copy, row in pairs if copy["date"] == row["date"] + 1) == 1  # a second late
    # One deletion recorded only on the line's copy, and the voicemail is in Deleted
    only_line = [(copy, row) for copy, row in pairs if copy["flags"] & 0x8 and not row["flags"] & 0x8]
    assert len(only_line) == 1 and only_line[0][0]["trashed_date"]
    assert next(v for v in expected["voicemails"] if v["rowid"] == only_line[0][1]["ROWID"])["folder"] == "deleted"
    # One main row without audio whose line copy has it: the line's copy is the one listed
    paths = _paths(accessor)
    no_audio = [(copy, row) for copy, row in pairs if f"Library/Voicemail/{row['ROWID']}.amr" not in paths]
    assert len(no_audio) == 1
    assert f"Library/Voicemail/{line}/{no_audio[0][0]['ROWID']}.amr" in paths
    assert any(v["id"] == f"{line}.{no_audio[0][0]['ROWID']}" for v in expected["voicemails"])


def test_older_snapshots_have_no_line_store(made):
    for snapshot, password in [("ios15", None), ("ios17", PASSWORD)]:
        _, accessor = made[(snapshot, password)]
        assert _line_store(accessor) is None
        assert _rows(accessor, MAIN, "SELECT * FROM map") == []
        assert all(r["label"] is None for r in _rows(accessor, MAIN, "SELECT label FROM voicemail"))


# ── the pack format ──────────────────────────────────────────────────────────────


def _pack_with(tmp_path, voicemails: str, contacts_extra=None):
    folder = tmp_path / "packs" / "testpersona"
    shutil.copytree(PERSONAS_DIR / "testpersona", folder)
    (folder / "voicemails.txt").write_text(voicemails, encoding="utf-8")
    if contacts_extra:
        data = json.loads((folder / "persona.json").read_text(encoding="utf-8"))
        data["contacts"].append(contacts_extra)
        (folder / "persona.json").write_text(json.dumps(data), encoding="utf-8")
    return lambda: load_persona("testpersona", root=folder.parent)


def test_a_line_reads_its_flags_and_may_have_no_transcript(tmp_path):
    persona = _pack_with(tmp_path, "== 2024-01-01\n09:00 unknown 12s (spam) (unheard)\n"
                                   "10:00 riley 1m02s (deleted): Call me. (Soon.)\n")()
    first, second = persona.voicemails
    assert (first.who, first.duration, first.transcript, first.flags) == ("unknown", 12, "", {"spam", "unheard"})
    assert (second.transcript, second.flags) == ("Call me. (Soon.)", {"deleted"})


@pytest.mark.parametrize("line, message", [
    ("09:00 riley 12s (sapm): Hi", r"unknown voicemail flag \(sapm\)"),
    ("09:00 riley 12s (spam) (spam): Hi", "written twice"),
    ("09:00 riley 12s (deleted) (deleted-old): Hi", r"\(deleted\), \(deleted-old\) can't go together"),
    ("09:00 riley 12s (removed) (line): Hi", "can't go together"),
    ("09:00 riley 12s (audio-only) (no-audio)", "can't go together"),
    ("09:00 riley 12s (audio-only) (spam)", r"\(audio-only\), \(spam\) can't go together"),
    ("09:00 riley 12s (line-audio) (no-audio): Hi", "can't go together"),
    ("09:00 riley 12s (line-only) (line-late): Hi", "can't go together"),
    ("09:00 nobody 12s: Hi", "unknown contact 'nobody'"),
])
def test_a_wrong_line_says_what_is_wrong(tmp_path, line, message):
    with pytest.raises(PersonaError, match=message):
        _pack_with(tmp_path, f"== 2024-01-01\n{line}\n")()


def test_no_contact_may_be_called_unknown(tmp_path):
    load = _pack_with(tmp_path, "== 2024-01-01\n09:00 riley 12s: Hi\n",
                      {"id": "unknown", "first": "Un", "last": "Known", "phones": [["mobile", "+44 7700 900170"]]})
    with pytest.raises(PersonaError, match="can't have the id 'unknown'"):
        load()
    # Call history takes it for no number too, so a pack without voicemail can't have one either
    (tmp_path / "packs" / "testpersona" / "voicemails.txt").unlink()
    with pytest.raises(PersonaError, match="can't have the id 'unknown'"):
        load()


def test_several_line_words_go_together(tmp_path):
    """A line's copy can lack the "+" and be a second late at once, and hold the only audio: still one voicemail."""
    persona = _pack_with(tmp_path, "== 2025-03-19\n17:30 jordan 25s (line-no-plus) (line-late) (line-audio): Hi.\n"
                                   "17:45 riley 9s (line) (line-deleted): Gone.\n")()
    backup_dir = generate("testpersona", "ios26", tmp_path / "out", persona=persona)
    expected = json.loads((tmp_path / "out" / EXPECTED).read_text(encoding="utf-8"))
    accessor = open_local_backup(str(backup_dir))
    try:
        listed = VoicemailExtractor().list_voicemails(accessor, {}, include_deleted=True)["voicemails"]
        assert sorted((str(v["id"]), v["phone_number"], v["date_received"], v["folder"]) for v in listed) == sorted(
            (str(v["id"]), v["sender"], v["date"], v["folder"]) for v in expected["voicemails"])
        jordan = next(v for v in expected["voicemails"] if v["sender"] == "447700900003")  # the copy, without its "+"
        assert str(jordan["id"]).startswith("A-") and jordan["date"].endswith(":41+00:00")
        assert next(v for v in expected["voicemails"] if v["sender"] == "+447700900002")["folder"] == "deleted"
    finally:
        accessor.cleanup()


def test_a_line_copy_never_has_its_main_rows_rowid(tmp_path, monkeypatch):
    # The line's first ROWID is drawn as its main row's (5), so the writer has to move it on
    monkeypatch.setattr(voicemail_writer, "_LINE_START", (5, 6))
    persona = _pack_with(tmp_path, "== 2025-03-19\n" + "".join(f"1{n}:00 riley {n + 5}s: Hi.\n" for n in range(4))
                         + "17:30 jordan 25s (line): Hi.\n")()
    backup_dir = generate("testpersona", "ios26", tmp_path / "out", persona=persona)
    accessor = open_local_backup(str(backup_dir))
    try:
        line, relative = _line_store(accessor)
        (copy,) = _rows(accessor, relative, "SELECT ROWID, date FROM voicemail")
        (main,) = _rows(accessor, MAIN, f"SELECT ROWID FROM voicemail WHERE date = {copy['date']}")
        assert copy["ROWID"] != main["ROWID"] and copy["ROWID"] > 1
    finally:
        accessor.cleanup()


def test_the_report_shows_every_voicemail_and_how_it_is_kept():
    from backupgen.report import render

    text = render(load_persona("testpersona"))
    assert "| no number | 0m12s |" in text and "| +447700900160 | 0m28s (line-late) (spam) |" in text


def test_a_deletion_after_the_backup_isnt_in_it(tmp_path):
    """(deleted) deletes it a little while after it came in: a backup taken before then still lists it."""
    persona = _pack_with(tmp_path, "== 2024-02-20\n20:55 riley 12s (deleted): Just before the backup.\n")()
    generate("testpersona", "ios17", tmp_path / "out", persona=persona)  # backed up 2024-02-20 21:00
    (voicemail,) = json.loads((tmp_path / "out" / EXPECTED).read_text(encoding="utf-8"))["voicemails"]
    assert (voicemail["folder"], voicemail["is_deleted"]) == ("voicemail", False)
