"""A voicemail's transcript, from the <ROWID>.transcript file beside its audio.

No voicemail.db schema we hold (iOS 13 to 17) has a transcript column. The text
is in a file per voicemail: an NSKeyedArchiver
archive of VisualVoicemail.framework's VMVoicemailTranscript, whose
``transcriptionString`` is what the Phone app shows. The archives here are
built as that class's ``encodeWithCoder:`` writes them (iOS 18.2).
"""

import os
import plistlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from backupgen.writers.voicemail import transcript_archive  # noqa: E402
from ios_backup_core.extractors.calls import CallExtractor  # noqa: E402
from ios_backup_core.extractors.voicemail import (  # noqa: E402
    VoicemailExtractor,
    read_transcript,
    transcript_text,
)
from tests.test_voicemail_folders import (  # noqa: E402
    ACCOUNT,
    LIVE,
    MAIN,
    SPAM,
    TRASHED,
    _audio,
    _Backup,
    _list,
    _store,
    _two_stores,
)

WARRANTY = "This is an important message about your vehicle's extended warranty. Press one now."


def _transcripts(tmp_path, folder, texts):
    """texts: {ROWID: transcript text or raw bytes}"""
    files = {}
    for rowid, text in texts.items():
        local = tmp_path / "transcripts" / folder.replace("/", "_") / f"{rowid}.transcript"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(text if isinstance(text, bytes) else transcript_archive(text, 20))
        files[f"{folder}/{rowid}.transcript"] = str(local)
    return files


def _one_store(tmp_path, texts):
    db = _store(tmp_path / "main" / "voicemail.db", [
        (1, 9001, 1_700_000_000, "+15550000001", 50, LIVE, 0),
        (2, 9002, 1_700_100_000, "+15550000002", 31, SPAM, 0),
        (3, 9003, 1_700_200_000, "+15550000003", 12, TRASHED, 1_700_200_500),
    ])
    return _Backup({
        MAIN: db, **_audio(tmp_path, "Library/Voicemail", 1, 2, 3),
        **_transcripts(tmp_path, "Library/Voicemail", texts),
    })


def test_the_archived_transcript_is_read():
    assert transcript_text(transcript_archive(WARRANTY, 20)) == WARRANTY


def test_every_list_shows_its_transcript(tmp_path):
    backup = _one_store(tmp_path, {1: "Call me back about the release.", 2: WARRANTY, 3: "See you at seven."})
    result = _list(backup, include_deleted=True)
    assert {v["id"]: v["transcript"] for v in result["voicemails"]} == {
        3: "See you at seven.", 2: WARRANTY, 1: "Call me back about the release.",
    }


def test_a_voicemail_never_transcribed_has_none(tmp_path):
    backup = _one_store(tmp_path, {2: WARRANTY})
    assert {v["id"]: v["transcript"] for v in _list(backup)["voicemails"]} == {2: WARRANTY, 1: ""}


def test_a_damaged_transcript_leaves_the_rest(tmp_path):
    archive = transcript_archive(WARRANTY, 20)
    backup = _one_store(tmp_path, {1: archive[: len(archive) // 2], 2: WARRANTY})
    assert {v["id"]: v["transcript"] for v in _list(backup)["voicemails"]} == {2: WARRANTY, 1: ""}


def test_a_transcript_listed_but_not_extracted_is_empty(tmp_path):
    backup = _one_store(tmp_path, {2: WARRANTY})
    backup.files["Library/Voicemail/2.transcript"] = str(tmp_path / "gone.transcript")
    assert [v["transcript"] for v in _list(backup)["voicemails"]] == ["", ""]


def test_the_account_store_has_its_own_transcripts(tmp_path):
    files = _two_stores(tmp_path)
    files.update(_transcripts(tmp_path, f"Library/Voicemail/{ACCOUNT}", {13: "Only in the account store."}))
    files.update(_transcripts(tmp_path, "Library/Voicemail", {13: "Main store's ROWID 13 is no voicemail."}))
    result = _list(_Backup(files))
    assert result["voicemails"][0]["id"] == f"{ACCOUNT}.13"
    assert result["voicemails"][0]["transcript"] == "Only in the account store."


def test_a_copy_without_a_transcript_takes_the_other_copys(tmp_path):
    # Main store's 24 is listed (it has audio); only its account copy, 12, was transcribed.
    files = _two_stores(tmp_path)
    files.update(_transcripts(tmp_path, f"Library/Voicemail/{ACCOUNT}", {12: "Transcribed in the other store."}))
    by_id = {v["id"]: v["transcript"] for v in _list(_Backup(files))["voicemails"]}
    assert by_id[24] == "Transcribed in the other store."


def test_counting_voicemails_opens_no_transcript(tmp_path):
    backup = _one_store(tmp_path, {1: "Call me back.", 2: WARRANTY})
    opened = []
    get_file = backup.get_file
    backup.get_file = lambda path, domain="HomeDomain": opened.append(path) or get_file(path, domain)
    rows = VoicemailExtractor().voicemail_rows(backup)["rows"]
    CallExtractor().list_calls(backup, {}, 0, 100)
    assert {r["id"]: r["transcript_file"] for r in rows} == {
        3: None, 2: "Library/Voicemail/2.transcript", 1: "Library/Voicemail/1.transcript",
    }
    assert not any(p.endswith(".transcript") for p in opened)


def test_deleted_ones_left_out_are_not_read(tmp_path):
    backup = _one_store(tmp_path, {3: "Deleted."})
    opened = []
    get_file = backup.get_file
    backup.get_file = lambda path, domain="HomeDomain": opened.append(path) or get_file(path, domain)
    _list(backup)
    assert "Library/Voicemail/3.transcript" not in opened


def _keyed(root_fields, *extra):
    objects = ["$null", {**root_fields, "$class": plistlib.UID(2)},
               {"$classname": "VMVoicemailTranscript", "$classes": ["VMVoicemailTranscript", "NSObject"]}, *extra]
    archive = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": plistlib.UID(1)},
               "$objects": objects}
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def test_a_mutable_string_is_read():
    data = _keyed({"transcriptionString": plistlib.UID(3)},
                  {"NS.string": "Mutable text.", "$class": plistlib.UID(4)},
                  {"$classname": "NSMutableString", "$classes": ["NSMutableString", "NSString", "NSObject"]})
    assert transcript_text(data) == "Mutable text."


def test_a_nil_transcript_is_empty():
    assert transcript_text(_keyed({"transcriptionString": plistlib.UID(0), "confidence": 0.0})) == ""


def test_a_plain_plist_is_read():
    data = plistlib.dumps({"transcriptionString": " Plain. ", "confidence": 0.8})
    assert transcript_text(data) == "Plain."


def test_what_isnt_a_transcript_is_empty():
    for data in (b"", b"#!AMR\n", plistlib.dumps(["a list"]), _keyed({"confidence": 0.5}),
                 _keyed({"transcriptionString": plistlib.UID(99)})):
        assert transcript_text(data) == ""


def test_no_path_reads_nothing():
    assert read_transcript(_Backup({}), None) == ""
    assert read_transcript(_Backup({}), "Library/Voicemail/1.transcript") == ""
