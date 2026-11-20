"""Tests for ios_backup_core.mp4: Voice Memos transcripts, tags and codec.

Every file here is built in the test with a small box writer, laid out the way
Voice Memos (through AVFoundation) writes them: an .m4a with the transcript in
``moov/trak/udta/tsrp`` and iTunes tags in ``moov/udta/meta/ilst``, and a
QuickTime .qta with the transcript as the ``com.apple.VoiceMemos.tsrp`` item of
``moov/trak/meta``. The audio data is filler: nothing here decodes audio. All
text is made up.
"""

import io
import json
import os
import random
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core import mp4
from ios_backup_core.mp4 import (
    audio_codec,
    parse_transcript,
    read_tags,
    read_transcript,
)

# ---------------------------------------------------------------------------
# A minimal box writer
# ---------------------------------------------------------------------------


def box(kind: bytes, *payload: bytes) -> bytes:
    body = b"".join(payload)
    return struct.pack(">I", 8 + len(body)) + kind + body


def box64(kind: bytes, *payload: bytes) -> bytes:
    """A box with a 64-bit size: size field 1, then the size in 8 bytes."""
    body = b"".join(payload)
    return struct.pack(">I", 1) + kind + struct.pack(">Q", 16 + len(body)) + body


def box0(kind: bytes, *payload: bytes) -> bytes:
    """A box of size 0: it runs to the end of whatever holds it."""
    return struct.pack(">I", 0) + kind + b"".join(payload)


def full(kind: bytes, flags: int, *payload: bytes) -> bytes:
    """A full box: a version/flags word before the payload."""
    return box(kind, struct.pack(">I", flags), *payload)


def tkhd(enabled=True) -> bytes:
    return full(b"tkhd", 0x000001 if enabled else 0, bytes(80))


def hdlr(handler=b"soun", quicktime=False) -> bytes:
    component = b"mhlr" if quicktime else bytes(4)
    return full(b"hdlr", 0, component, handler, bytes(12), b"Core Media Audio\x00")


def stsd(*codecs: bytes) -> bytes:
    entries = [struct.pack(">I", 36) + codec + bytes(28) for codec in codecs]
    return full(b"stsd", 0, struct.pack(">I", len(entries)), *entries)


def trak(handler=b"soun", enabled=True, codec=b"mp4a", extra=(), quicktime=False) -> bytes:
    stbl = box(b"stbl", stsd(codec) if codec else b"", box(b"stts", bytes(8)))
    minf = box(b"minf", box(b"smhd", bytes(8)), box(b"dinf", box(b"dref", bytes(8))), stbl)
    mdia = box(b"mdia", box(b"mdhd", bytes(24)), hdlr(handler, quicktime), minf)
    return box(b"trak", tkhd(enabled), mdia, *extra)


def tsrp(doc) -> bytes:
    """The .m4a transcript: ``udta/tsrp`` holding UTF-8 JSON."""
    raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode("utf-8")
    return box(b"udta", box(b"tsrp", raw))


def data(value: bytes, type_code=1) -> bytes:
    return box(b"data", struct.pack(">I", type_code), bytes(4), value)


def qt_meta(items: dict, with_version=False) -> bytes:
    """QuickTime ``meta``: hdlr 'mdta', ``keys`` and ``ilst`` items numbered from 1."""
    keys = [struct.pack(">I", 8 + len(k)) + b"mdta" + k for k in items]
    entries = [box(struct.pack(">I", i), data(v, 0)) for i, v in enumerate(items.values(), start=1)]
    body = (full(b"hdlr", 0, bytes(4), b"mdta", bytes(12), b"\x00"),
            full(b"keys", 0, struct.pack(">I", len(keys)), *keys),
            box(b"ilst", *entries))
    return full(b"meta", 0, *body) if with_version else box(b"meta", *body)


def qt_transcript(doc, **kw) -> bytes:
    raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode("utf-8")
    return qt_meta({b"com.apple.quicktime.other": b"x", b"com.apple.VoiceMemos.tsrp": raw}, **kw)


def freeform(mean: bytes, name: bytes, value: bytes) -> bytes:
    return box(b"----", full(b"mean", 0, mean), full(b"name", 0, name), data(value))


def itunes_tags(*items: bytes) -> bytes:
    """``moov/udta/meta/ilst``, an ISO meta box (with version/flags)."""
    return box(b"udta", full(b"meta", 0, full(b"hdlr", 0, bytes(4), b"mdir", b"appl", bytes(9)),
                              box(b"ilst", *items)))


def tags(title=None, encoder=None, unique_id=None) -> bytes:
    items = [freeform(b"com.apple.iTunes", b"iTunSMPB", b" 00000000 00000840")]
    if title is not None:
        items.append(box(b"\xa9nam", data(title.encode("utf-8"))))
    if encoder is not None:
        items.append(box(b"\xa9too", data(encoder.encode("utf-8"))))
    if unique_id is not None:
        items.append(freeform(b"com.apple.iTunes", b"voice-memo-uuid", unique_id.encode("ascii")))
    return itunes_tags(*items)


def m4a(*tracks: bytes, udta=b"", audio=bytes(256), moov_first=True) -> bytes:
    ftyp = box(b"ftyp", b"M4A ", bytes(4), b"M4A mp42isom")
    moov = box(b"moov", box(b"mvhd", bytes(100)), *(tracks or (trak(),)), udta)
    mdat = box(b"mdat", audio)
    return ftyp + (moov + box(b"free", bytes(16)) + mdat if moov_first else mdat + moov)


def qta(*tracks: bytes, audio=bytes(256)) -> bytes:
    """QuickTime layout: ftyp 'qt  ', wide, mdat, then moov."""
    moov = box(b"moov", box(b"mvhd", bytes(100)), *tracks)
    return box(b"ftyp", b"qt  ", bytes(4), b"qt  ") + box(b"wide") + box(b"mdat", audio) + moov


# Transcript JSON, both shapes. Shape A: runs index an attribute table.
SHAPE_A = {
    "attributedString": {
        "attributeTable": [{"timeRange": [0, 0.84]}, {"timeRange": [0.84, 1.5]}, {"timeRange": [1.5, 2.25]}],
        "runs": ["Check", 0, " the north", 1, " stairwell.", 2],
    },
    "locale": {"identifier": "en_US", "current": 0},
}
# Shape B: the runs carry their attributes inline.
SHAPE_B = {
    "attributedString": ["Tile", {"timeRange": [0, 0.4]}, " samples", {"timeRange": [0.4, 1.1]}],
    "locale": {"identifier": "en_GB"},
}


def write(tmp_path, name, content: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

def test_m4a_transcript_shape_a(tmp_path):
    path = write(tmp_path, "memo.m4a", m4a(trak(extra=[tsrp(SHAPE_A)])))
    assert read_transcript(path) == {
        "text": "Check the north stairwell.",
        "segments": [
            {"text": "Check", "start": 0.0, "end": 0.84},
            {"text": " the north", "start": 0.84, "end": 1.5},
            {"text": " stairwell.", "start": 1.5, "end": 2.25},
        ],
        "locale": "en_US",
    }


def test_m4a_transcript_shape_b(tmp_path):
    path = write(tmp_path, "memo.m4a", m4a(trak(extra=[tsrp(SHAPE_B)])))
    result = read_transcript(path)
    assert result["text"] == "Tile samples"
    assert result["segments"] == [{"text": "Tile", "start": 0.0, "end": 0.4},
                                  {"text": " samples", "start": 0.4, "end": 1.1}]
    assert result["locale"] == "en_GB"


@pytest.mark.parametrize("with_version", [False, True])
def test_qta_transcript_in_quicktime_metadata(tmp_path, with_version):
    path = write(tmp_path, "memo.qta", qta(trak(quicktime=True, extra=[qt_transcript(SHAPE_A, with_version=with_version)])))
    result = read_transcript(path)
    assert result["text"] == "Check the north stairwell."
    assert len(result["segments"]) == 3


def test_transcript_ignores_other_metadata_keys(tmp_path):
    meta = qt_meta({b"com.apple.quicktime.title": b'{"attributedString": ["Not this"]}'})
    path = write(tmp_path, "memo.qta", qta(trak(quicktime=True, extra=[meta])))
    assert read_transcript(path) is None


def test_ilst_item_without_a_key_is_ignored(tmp_path):
    keys = full(b"keys", 0, struct.pack(">I", 1), struct.pack(">I", 8 + 25) + b"mdta" + b"com.apple.VoiceMemos.tsrp")
    ilst = box(b"ilst", box(struct.pack(">I", 7), data(json.dumps(SHAPE_B).encode(), 0)),
               box(struct.pack(">I", 1), data(json.dumps(SHAPE_A).encode(), 0)))
    meta = box(b"meta", full(b"hdlr", 0, bytes(4), b"mdta", bytes(13)), keys, ilst)
    path = write(tmp_path, "memo.qta", qta(trak(quicktime=True, extra=[meta])))
    assert read_transcript(path)["text"] == "Check the north stairwell."


def test_first_enabled_sound_track_wins(tmp_path):
    old = {"attributedString": ["Disabled take", {"timeRange": [0, 1]}]}
    new = {"attributedString": ["Enabled take", {"timeRange": [0, 1]}]}
    video = {"attributedString": ["Video track", {"timeRange": [0, 1]}]}
    path = write(tmp_path, "memo.m4a", m4a(
        trak(handler=b"vide", codec=b"avc1", extra=[tsrp(video)]),
        trak(enabled=False, extra=[tsrp(old)]),
        trak(enabled=True, extra=[tsrp(new)]),
    ))
    assert read_transcript(path)["text"] == "Enabled take"


def test_long_handler_names(tmp_path):
    name = b"A very long component name that some encoders write into hdlr\x00"
    track = box(b"trak", tkhd(), box(b"mdia", full(b"hdlr", 0, b"mhlr", b"soun", bytes(12), name),
                                     box(b"minf", box(b"stbl", stsd(b"alac")))), tsrp(SHAPE_B))
    path = write(tmp_path, "memo.qta", qta(track))
    assert read_transcript(path)["text"] == "Tile samples"
    assert audio_codec(path) == "alac"


def test_disabled_sound_track_is_still_read(tmp_path):
    path = write(tmp_path, "memo.m4a", m4a(trak(enabled=False, extra=[tsrp(SHAPE_B)])))
    assert read_transcript(path)["text"] == "Tile samples"


def test_no_transcript_on_a_video_track_only(tmp_path):
    path = write(tmp_path, "memo.m4a", m4a(trak(handler=b"vide", codec=b"avc1", extra=[tsrp(SHAPE_A)])))
    assert read_transcript(path) is None


def test_file_without_a_transcript(tmp_path):
    path = write(tmp_path, "memo.m4a", m4a(trak(), udta=tags(title="Plain")))
    assert read_transcript(path) is None


@pytest.mark.parametrize("payload", [
    b"",                                            # empty box
    b"not json at all",
    b'{"attributedString": ',                       # cut short
    b'["a list", "not an object"]',
    json.dumps({"attributedString": ""}).encode(),  # an empty transcript
    json.dumps({"attributedString": {"runs": [], "attributeTable": []}}).encode(),
    json.dumps({"attributedString": {"runs": ["   ", 0], "attributeTable": [{"timeRange": [0, 1]}]}}).encode(),
    json.dumps({"something": "else"}).encode(),
    json.dumps({"attributedString": 42}).encode(),
])
def test_empty_or_damaged_transcripts_give_none(tmp_path, payload):
    path = write(tmp_path, "memo.m4a", m4a(trak(extra=[tsrp(payload)])))
    assert read_transcript(path) is None


def test_padding_around_the_json_and_bad_utf8_are_tolerated(tmp_path):
    raw = b"\x00\x00\x00\x00" + json.dumps(SHAPE_B).encode()[:-1] + b',"note":"caf\xe9"}' + b"\x00" * 8
    path = write(tmp_path, "memo.m4a", m4a(trak(extra=[tsrp(raw)])))
    assert read_transcript(path)["text"] == "Tile samples"


def test_parse_transcript_odd_runs():
    # A trailing run without attributes, a string locale, an index out of
    # range, a non-numeric time range and a bool masquerading as an index.
    doc = {
        "attributedString": {
            "attributeTable": [{"timeRange": [0, 1]}, {"timeRange": ["a", 2]}],
            "runs": ["One", 0, " two", 1, " three", 9, " four", True, " five"],
        },
        "locale": "de_DE",
    }
    result = parse_transcript(doc)
    assert result["text"] == "One two three four five"
    assert result["segments"] == [{"text": "One", "start": 0.0, "end": 1.0}]
    assert result["locale"] == "de_DE"


def test_parse_transcript_resynchronises_and_skips_bad_values():
    doc = {"attributedString": [7, "Hello", {"timeRange": [0, float("inf")]}, "there", " friend",
                                {"timeRange": [1, 2]}], "locale": {"identifier": ""}}
    result = parse_transcript(doc)
    assert result["text"] == "Hellothere friend"
    assert result["segments"] == [{"text": " friend", "start": 1.0, "end": 2.0}]
    assert result["locale"] is None
    assert parse_transcript(None) is None and parse_transcript([]) is None


def test_plain_string_transcript():
    assert parse_transcript({"attributedString": "  Just text  "}) == {
        "text": "Just text", "segments": [], "locale": None}


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def test_read_tags(tmp_path):
    encoder = "com.apple.VoiceMemos (iPhone Version 26.5 (Build 23F77))"
    path = write(tmp_path, "memo.m4a", m4a(udta=tags(
        title="Café walk-through", encoder=encoder, unique_id="6F3B2C1A-0D4E-4A51-9C77-3E2B1F0A9D88")))
    assert read_tags(path) == {
        "title": "Café walk-through",
        "encoder": encoder,
        "unique_id": "6F3B2C1A-0D4E-4A51-9C77-3E2B1F0A9D88",
    }


def test_read_tags_only_what_is_there(tmp_path):
    assert read_tags(write(tmp_path, "a.m4a", m4a(udta=tags(title="Only a title")))) == {"title": "Only a title"}
    assert read_tags(write(tmp_path, "b.m4a", m4a(udta=tags()))) == {}
    assert read_tags(write(tmp_path, "c.m4a", m4a())) == {}


def test_freeform_items_must_be_the_voice_memo_uuid(tmp_path):
    path = write(tmp_path, "memo.m4a", m4a(udta=itunes_tags(
        freeform(b"com.example.other", b"voice-memo-uuid", b"WRONG-MEAN"),
        freeform(b"com.apple.iTunes", b"voice-memo-plrt", b"0.5"),
        freeform(b"com.apple.iTunes", b"voice-memo-uuid", b"RIGHT"),
        box(b"\xa9nam", data(b"   ")),           # blank: not a title
        box(b"\xa9nam", data(b"Second title")),  # the first non-blank one is used
    )))
    assert read_tags(path) == {"unique_id": "RIGHT", "title": "Second title"}


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("codec", [b"mp4a", b"alac", b"apac"])
def test_audio_codec(tmp_path, codec):
    assert audio_codec(write(tmp_path, "memo.m4a", m4a(trak(codec=codec)))) == codec.decode()


def test_audio_codec_prefers_the_sound_track(tmp_path):
    path = write(tmp_path, "memo.qta", qta(trak(handler=b"vide", codec=b"avc1"),
                                           trak(quicktime=True, codec=b"alac")))
    assert audio_codec(path) == "alac"
    # With no sound track, the first track's codec is still worth knowing.
    assert audio_codec(write(tmp_path, "video.mov", qta(trak(handler=b"vide", codec=b"avc1")))) == "avc1"


def test_audio_codec_none_when_there_is_none(tmp_path):
    assert audio_codec(write(tmp_path, "a.m4a", m4a(trak(codec=None)))) is None
    empty_stsd = box(b"trak", tkhd(), box(b"mdia", hdlr(), box(b"minf", box(b"stbl", full(b"stsd", 0, bytes(4))))))
    assert audio_codec(write(tmp_path, "b.m4a", m4a(empty_stsd))) is None
    assert audio_codec(write(tmp_path, "c.m4a", m4a(trak(codec=b"\x00\x01\x02\x03")))) is None


# ---------------------------------------------------------------------------
# Layout: 64-bit and size-0 boxes, moov after mdat, never reading mdat
# ---------------------------------------------------------------------------

def test_moov_after_mdat(tmp_path):
    path = write(tmp_path, "memo.m4a", m4a(trak(extra=[tsrp(SHAPE_A)]), udta=tags(title="Late moov"),
                                           moov_first=False))
    assert read_transcript(path)["text"] == "Check the north stairwell."
    assert read_tags(path) == {"title": "Late moov"}
    assert audio_codec(path) == "mp4a"


def test_64_bit_box_sizes(tmp_path):
    track = box64(b"trak", tkhd(), box64(b"mdia", hdlr(), box(b"minf", box(b"stbl", stsd(b"alac")))),
                  box64(b"udta", box64(b"tsrp", json.dumps(SHAPE_B).encode())))
    content = (box(b"ftyp", b"M4A ", bytes(4)) + box64(b"mdat", bytes(512))
               + box64(b"moov", box(b"mvhd", bytes(100)), track, tags(title="Wide boxes")))
    path = write(tmp_path, "memo.m4a", content)
    assert read_transcript(path)["text"] == "Tile samples"
    assert read_tags(path) == {"title": "Wide boxes"}
    assert audio_codec(path) == "alac"


def test_size_zero_boxes_run_to_the_end(tmp_path):
    # moov last, with size 0; inside it the last trak and its tsrp have size 0 too.
    track = box0(b"trak", tkhd(), box(b"mdia", hdlr(), box(b"minf", box(b"stbl", stsd(b"mp4a")))),
                 box(b"udta", box0(b"tsrp", json.dumps(SHAPE_A).encode())))
    content = box(b"ftyp", b"M4A ", bytes(4)) + box(b"mdat", bytes(64)) + box0(b"moov", box(b"mvhd", bytes(100)), track)
    path = write(tmp_path, "memo.m4a", content)
    assert read_transcript(path)["text"] == "Check the north stairwell."
    assert audio_codec(path) == "mp4a"
    # An mdat of size 0 at the end, after the moov.
    path = write(tmp_path, "tail.m4a", box(b"ftyp", b"M4A ", bytes(4))
                 + box(b"moov", box(b"mvhd", bytes(100)), trak(extra=[tsrp(SHAPE_B)])) + box0(b"mdat", bytes(4096)))
    assert read_transcript(path)["text"] == "Tile samples"


class _CountingFile(io.FileIO):
    """A file that counts the bytes read from it."""

    total = 0

    def read(self, size=-1):
        chunk = super().read(size)
        _CountingFile.total += len(chunk)
        return chunk


def test_the_audio_data_is_never_read(tmp_path, monkeypatch):
    audio = bytes(4 * 1024 * 1024)
    path = write(tmp_path, "long.m4a", m4a(trak(extra=[tsrp(SHAPE_A)]), udta=tags(title="Long"),
                                           audio=audio, moov_first=False))
    monkeypatch.setattr(mp4, "open", lambda p, mode="rb": _CountingFile(p, "r"), raising=False)
    for read in (read_transcript, read_tags, audio_codec):
        _CountingFile.total = 0
        assert read(path)
        assert _CountingFile.total < 16 * 1024, read.__name__


# ---------------------------------------------------------------------------
# Damaged and unrelated files
# ---------------------------------------------------------------------------

def _all_readers(path):
    return read_transcript(path), read_tags(path), audio_codec(path)


def test_missing_and_odd_paths():
    assert _all_readers("/no/such/file.m4a") == (None, {}, None)
    assert _all_readers(None) == (None, {}, None)


def test_empty_and_non_mp4_files(tmp_path):
    assert _all_readers(write(tmp_path, "empty.m4a", b"")) == (None, {}, None)
    assert _all_readers(write(tmp_path, "text.m4a", b"Hello, this is not audio.\n" * 50)) == (None, {}, None)
    assert _all_readers(write(tmp_path, "zeros.m4a", bytes(10_000))) == (None, {}, None)


def test_no_moov(tmp_path):
    content = box(b"ftyp", b"M4A ", bytes(4)) + box(b"free", bytes(32)) + box(b"mdat", bytes(128))
    assert _all_readers(write(tmp_path, "nomoov.m4a", content)) == (None, {}, None)


def test_garbage_bytes_never_raise(tmp_path):
    rng = random.Random(20261120)
    for i in range(200):
        content = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 600)))
        if i % 3 == 0:  # garbage behind a plausible start
            content = box(b"ftyp", b"M4A ", bytes(4)) + struct.pack(">I", rng.randrange(0, 2000)) + b"moov" + content
        transcript, found_tags, codec = _all_readers(write(tmp_path, f"g{i}.m4a", content))
        assert transcript is None or isinstance(transcript, dict)
        assert isinstance(found_tags, dict)
        assert codec is None or isinstance(codec, str)


def test_truncated_files_never_raise(tmp_path):
    whole = m4a(trak(extra=[tsrp(SHAPE_A)]), udta=tags(title="Cut short", unique_id="ABCD"), moov_first=False)
    for cut in range(0, len(whole), 7):
        # The moov is last, so every cut leaves it short, and a short moov is never read.
        assert _all_readers(write(tmp_path, "cut.m4a", whole[:cut])) == (None, {}, None), cut
    # moov before mdat, cut inside the audio: everything is still readable.
    early = m4a(trak(extra=[tsrp(SHAPE_A)]), udta=tags(title="Cut short"), audio=bytes(4096))
    path = write(tmp_path, "early.m4a", early[:-1000])
    assert read_transcript(path)["text"] == "Check the north stairwell."
    assert read_tags(path) == {"title": "Cut short"}


@pytest.mark.parametrize("bad_size", [0, 3, 7, 1])
def test_impossible_box_sizes_stop_quietly(tmp_path, bad_size):
    # A child of trak claiming a size below its header (or a 64-bit size cut off).
    bad = struct.pack(">I", bad_size) + b"udta" + (b"" if bad_size == 1 else bytes(4))
    track = box(b"trak", tkhd(), box(b"mdia", hdlr(), box(b"minf", box(b"stbl", stsd(b"mp4a")))), bad[:12])
    path = write(tmp_path, "bad.m4a", box(b"ftyp", b"M4A ") + box(b"moov", track))
    assert read_transcript(path) is None
    assert audio_codec(path) == "mp4a"


def test_box_larger_than_its_parent(tmp_path):
    lying = struct.pack(">I", 10_000) + b"tsrp" + json.dumps(SHAPE_A).encode()
    path = write(tmp_path, "lying.m4a", m4a(trak(extra=[box(b"udta", lying)])))
    assert read_transcript(path) is None


def test_damaged_keys_box(tmp_path):
    keys = full(b"keys", 0, struct.pack(">I", 5), struct.pack(">I", 3) + b"mdta")  # entry size below 8
    ilst = box(b"ilst", box(struct.pack(">I", 1), data(json.dumps(SHAPE_A).encode(), 0)))
    meta = box(b"meta", full(b"hdlr", 0, bytes(4), b"mdta", bytes(13)), keys, ilst)
    path = write(tmp_path, "memo.qta", qta(trak(quicktime=True, extra=[meta])))
    assert read_transcript(path) is None


def test_many_tiny_boxes_finish_quickly(tmp_path):
    content = box(b"free") * 50_000 + box(b"moov", trak(extra=[tsrp(SHAPE_A)]))
    # Past the walking limit, so the moov isn't reached; what matters is that it returns.
    assert read_transcript(write(tmp_path, "tiny.m4a", content)) is None
