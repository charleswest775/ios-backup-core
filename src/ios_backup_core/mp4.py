"""
Read the few things a Voice Memos recording carries inside its audio file.

Voice Memos saves ``.m4a`` files (MPEG-4) and, for layered and spatial
recordings, ``.qta`` / ``.qt`` files (QuickTime). Both are trees of boxes
("atoms"): a 4-byte big-endian size, a 4-byte type, then the payload. This
module walks those headers with ``seek`` and small reads only. It never reads
the audio data (``mdat``), so a recording of any length costs a few kilobytes
of reading. Standard library only.

What it reads
-------------
- ``read_transcript(path)``: the transcript Voice Memos writes on iOS 18 and
  later. It is a metadata item on the first enabled sound track, not a column
  of CloudRecordings.db:

  - ``.m4a``: the box ``moov/trak/udta/tsrp`` (ISO user data), whose payload
    is UTF-8 JSON.
  - ``.qta`` / ``.qt``: QuickTime metadata ``moov/trak/meta``, with no
    version/flags word, holding ``keys`` and ``ilst``. The ``ilst`` item whose
    1-based index names the key ``com.apple.VoiceMemos.tsrp`` has a ``data``
    box: 4 bytes of type, 4 of locale, then the same JSON.

  The JSON comes in two shapes. Shape A::

      {"attributedString": {"attributeTable": [{"timeRange": [0, 2.52]}, ...],
                            "runs": ["OK,", 0, " I", 1, ...]},
       "locale": {"identifier": "en_US", "current": 0}}

  Shape B has the runs as ``attributedString`` itself, with each attribute
  inline: ``["OK,", {"timeRange": [0, 2.52]}, " I", {...}, ...]``. The text is
  the string runs joined together.

- ``read_tags(path)``: the iTunes-style tags in ``moov/udta/meta/ilst``:
  ``title`` (``©nam``), ``encoder`` (``©too``, for example
  ``com.apple.VoiceMemos (iPhone Version 17.2.1 (Build 21C66))``) and
  ``unique_id`` (the freeform ``----`` item
  ``com.apple.iTunes:voice-memo-uuid``, which equals the recording's
  ``ZUNIQUEID``).

- ``audio_codec(path)``: the first sample entry of the sound track's ``stsd``
  box: ``"mp4a"`` (AAC, the default), ``"alac"`` (Lossless), ``"apac"``
  (spatial audio), and so on.

Where these live was read from Apple's VoiceMemos.framework (iOS 26.5) and
checked on files written with AVFoundation the way Voice Memos writes them.
It has not been checked against a recording taken from a real iPhone backup
yet.

Damaged files
-------------
Every function returns ``None`` (``{}`` for tags) for a missing, truncated,
damaged or unrelated file, and never raises. Boxes with a 64-bit size (size
field 1, then an 8-byte size) and boxes of size 0 ("to the end of the
enclosing box") are read as the format defines them. A box that claims to run
past the end of its parent ends the walk at that level.
"""

import json
import math
import os
import struct

# A damaged file can look like a long run of tiny boxes; stop walking a level
# after this many rather than crawl through megabytes of noise.
_MAX_BOXES = 10_000

# The most a single payload (a transcript, a tag value) may be read into memory.
# An hour-long transcript is a few hundred kilobytes.
_MAX_PAYLOAD = 32 * 1024 * 1024

_TRANSCRIPT_KEY = "com.apple.VoiceMemos.tsrp"
_UUID_NAME = "voice-memo-uuid"
_ITUNES_MEAN = "com.apple.iTunes"

# Children a QuickTime-style ``meta`` box (no version/flags word) can start with.
_META_CHILDREN = {b"hdlr", b"keys", b"ilst", b"free", b"skip", b"xml ", b"bxml", b"iloc",
                  b"pitm", b"iinf", b"dinf", b"idat", b"iref", b"ipro", b"ctry", b"lang"}


# ---------------------------------------------------------------------------
# Box walking
# ---------------------------------------------------------------------------

def _boxes(f, start: int, end: int):
    """Yield (type, payload_start, payload_end) for each box in [start, end)."""
    pos = start
    count = 0
    while pos + 8 <= end and count < _MAX_BOXES:
        f.seek(pos)
        header = f.read(16)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header[:8])
        header_len = 8
        if size == 1:
            if len(header) < 16:
                return
            size = struct.unpack(">Q", header[8:16])[0]
            header_len = 16
        elif size == 0:
            size = end - pos
        if size < header_len or pos + size > end:
            return  # truncated or damaged: stop quietly
        yield kind, pos + header_len, pos + size
        pos += size
        count += 1


def _child(f, start: int, end: int, kind: bytes):
    """(payload_start, payload_end) of the first child box of this type, or None."""
    for found, s, e in _boxes(f, start, end):
        if found == kind:
            return s, e
    return None


def _path(f, start: int, end: int, *kinds: bytes):
    """Follow a chain of child box types, e.g. _path(f, s, e, b"mdia", b"hdlr")."""
    span = (start, end)
    for kind in kinds:
        span = _child(f, *span, kind)
        if span is None:
            return None
    return span


def _read(f, start: int, end: int, limit: int = _MAX_PAYLOAD):
    """The bytes of [start, end), or None when that is more than ``limit``."""
    if end - start > limit or end < start:
        return None
    f.seek(start)
    return f.read(end - start)


def _top(f):
    """(payload_start, payload_end) of the file's ``moov`` box, or None."""
    size = os.fstat(f.fileno()).st_size
    return _child(f, 0, size, b"moov")


def _tracks(f, moov):
    """[(trak span, handler type, enabled)] in file order."""
    tracks = []
    for kind, s, e in _boxes(f, *moov):
        if kind != b"trak":
            continue
        hdlr = _path(f, s, e, b"mdia", b"hdlr")
        # hdlr payload: version/flags (4), pre_defined or QuickTime component type (4), handler (4).
        handler = (_read(f, hdlr[0], min(hdlr[0] + 12, hdlr[1]), limit=12) or b"")[8:12] if hdlr else b""
        tkhd = _child(f, s, e, b"tkhd")
        flags = _read(f, tkhd[0], min(tkhd[0] + 4, tkhd[1]), limit=4) if tkhd else b""
        enabled = len(flags) == 4 and bool(flags[3] & 1)
        tracks.append(((s, e), handler, enabled))
    return tracks


def _sound_tracks(f, moov) -> list:
    """Sound tracks' spans, enabled ones first (otherwise in file order)."""
    sound = [t for t in _tracks(f, moov) if t[1] == b"soun"]
    sound.sort(key=lambda t: not t[2])
    return [span for span, _handler, _enabled in sound]


def _meta_body(f, start: int, end: int) -> int:
    """Where a ``meta`` box's children start.

    ISO ``meta`` (in ``udta``) is a full box with a 4-byte version/flags word;
    QuickTime ``meta`` (in ``trak`` or ``moov``) is not. A QuickTime one starts
    straight away with a child box, so its first 4 bytes are a non-zero size
    and the next 4 a known child type.
    """
    first = _read(f, start, min(start + 8, end), limit=8) or b""
    if len(first) == 8 and first[:4] != b"\0\0\0\0" and first[4:8] in _META_CHILDREN:
        return start
    return start + 4


def _data_value(f, start: int, end: int):
    """(type code, value bytes) of a ``data`` box payload, or None."""
    payload = _read(f, start, end)
    if payload is None or len(payload) < 8:
        return None
    type_code = int.from_bytes(payload[1:4], "big")  # byte 0 is the version
    return type_code, payload[8:]  # 4 bytes of type, 4 of locale


def _qt_meta_items(f, start: int, end: int) -> dict:
    """{key name: value bytes} of a QuickTime ``meta`` box with ``keys`` and ``ilst``."""
    body = _meta_body(f, start, end)
    keys_box = _child(f, body, end, b"keys")
    ilst_box = _child(f, body, end, b"ilst")
    if not keys_box or not ilst_box:
        return {}
    keys = _read(f, *keys_box)
    if not keys or len(keys) < 8:
        return {}
    count = struct.unpack(">I", keys[4:8])[0]  # after version/flags
    names, pos = {}, 8
    for index in range(1, count + 1):
        if pos + 8 > len(keys):
            break
        size = struct.unpack(">I", keys[pos:pos + 4])[0]
        if size < 8 or pos + size > len(keys):
            break
        # size (4), namespace such as 'mdta' (4), then the key itself.
        names[index] = keys[pos + 8:pos + size].decode("utf-8", "replace")
        pos += size
    items = {}
    for kind, s, e in _boxes(f, *ilst_box):
        name = names.get(struct.unpack(">I", kind)[0])
        if name is None or name in items:
            continue
        data = _child(f, s, e, b"data")
        value = _data_value(f, *data) if data else None
        if value is not None:
            items[name] = value[1]
    return items


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

def _transcript_bytes(f, track) -> bytes | None:
    """The raw transcript payload of one track, or None."""
    tsrp = _path(f, *track, b"udta", b"tsrp")
    if tsrp:
        raw = _read(f, *tsrp)
        if raw:
            return raw
    meta = _child(f, *track, b"meta")
    if meta:
        return _qt_meta_items(f, *meta).get(_TRANSCRIPT_KEY) or None
    return None


def _json(raw: bytes):
    """The JSON object in a payload, starting at its first ``{``; None when there isn't one."""
    start = raw.find(b"{")
    if start < 0:
        return None
    text = raw[start:].decode("utf-8", "replace")
    try:
        doc, _end = json.JSONDecoder().raw_decode(text)  # padding after it is fine
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def parse_transcript(doc) -> dict | None:
    """``{"text", "segments", "locale"}`` from a transcript's JSON, or None when it holds no text.

    Reads both shapes (see the module docstring). ``segments`` are
    ``{"text", "start", "end"}`` for each run with a time range, in seconds,
    and join up to the text; ``locale`` is the recogniser's locale, such as
    ``"en_US"``, or None.
    """
    if not isinstance(doc, dict):
        return None
    attributed = doc.get("attributedString")
    table = None
    if isinstance(attributed, dict):
        runs = attributed.get("runs")
        table = attributed.get("attributeTable")
        runs = runs if isinstance(runs, list) else []
        table = table if isinstance(table, list) else []
    elif isinstance(attributed, list):
        runs = attributed
    elif isinstance(attributed, str):
        runs = [attributed]
    else:
        return None

    parts, segments = [], []
    i = 0
    while i < len(runs):
        text = runs[i]
        attribute = runs[i + 1] if i + 1 < len(runs) else None
        if not isinstance(text, str):
            i += 1  # out of step: resynchronise on the next string
            continue
        i += 2 if i + 1 < len(runs) and not isinstance(attribute, str) else 1
        parts.append(text)
        if table is not None and isinstance(attribute, int) and not isinstance(attribute, bool):
            attribute = table[attribute] if 0 <= attribute < len(table) else None
        time_range = attribute.get("timeRange") if isinstance(attribute, dict) else None
        if isinstance(time_range, list) and len(time_range) == 2:
            start, end = _number(time_range[0]), _number(time_range[1])
            if start is not None and end is not None:
                segments.append({"text": text, "start": start, "end": end})

    full = "".join(parts).strip()
    if not full:
        return None
    locale = doc.get("locale")
    if isinstance(locale, dict):
        locale = locale.get("identifier")
    locale = locale.strip() if isinstance(locale, str) and locale.strip() else None
    return {"text": full, "segments": segments, "locale": locale}


def read_transcript(path) -> dict | None:
    """The Voice Memos transcript in an .m4a/.qta/.qt file, or None.

    Returns ``{"text": str, "segments": [{"text", "start", "end"}],
    "locale": str | None}`` (see parse_transcript). The first enabled sound
    track is tried first, then any other sound track. None when the file has
    no transcript, an empty one, or can't be read.
    """
    try:
        with open(path, "rb") as f:
            moov = _top(f)
            if not moov:
                return None
            for track in _sound_tracks(f, moov):
                raw = _transcript_bytes(f, track)
                doc = _json(raw) if raw else None
                if doc is not None:
                    return parse_transcript(doc)
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Tags and codec
# ---------------------------------------------------------------------------

def _tag_text(value: bytes) -> str | None:
    text = value.decode("utf-8", "replace").replace("\x00", "").strip()
    return text or None


def read_tags(path) -> dict:
    """iTunes-style tags from ``moov/udta/meta/ilst``.

    Returns any of ``title`` (``©nam``), ``encoder`` (``©too``) and
    ``unique_id`` (``com.apple.iTunes:voice-memo-uuid``) that the file has;
    ``{}`` when it has none or can't be read.
    """
    tags = {}
    try:
        with open(path, "rb") as f:
            moov = _top(f)
            meta = moov and _path(f, *moov, b"udta", b"meta")
            if not meta:
                return {}
            ilst = _child(f, _meta_body(f, *meta), meta[1], b"ilst")
            if not ilst:
                return {}
            for kind, s, e in _boxes(f, *ilst):
                if kind == b"----":
                    mean = _child(f, s, e, b"mean")
                    name = _child(f, s, e, b"name")
                    data = _child(f, s, e, b"data")
                    mean_text = _tag_text((_read(f, *mean, limit=1024) or b"")[4:]) if mean else None
                    name_text = _tag_text((_read(f, *name, limit=1024) or b"")[4:]) if name else None
                    if (name_text != _UUID_NAME or mean_text not in (None, _ITUNES_MEAN)
                            or not data or "unique_id" in tags):
                        continue
                    key = "unique_id"
                elif kind == b"\xa9nam":
                    key, data = "title", _child(f, s, e, b"data")
                elif kind == b"\xa9too":
                    key, data = "encoder", _child(f, s, e, b"data")
                else:
                    continue
                if key in tags or not data:
                    continue
                value = _data_value(f, *data)
                text = _tag_text(value[1]) if value else None
                if text:
                    tags[key] = text
    except Exception:
        return {}
    return tags


def audio_codec(path) -> str | None:
    """The sound track's codec, from its first ``stsd`` sample entry: "mp4a", "alac", "apac", ...

    None when the file has no such track or can't be read.
    """
    try:
        with open(path, "rb") as f:
            moov = _top(f)
            if not moov:
                return None
            tracks = _sound_tracks(f, moov) or [span for span, _h, _e in _tracks(f, moov)]
            for track in tracks:
                stsd = _path(f, *track, b"mdia", b"minf", b"stbl", b"stsd")
                head = _read(f, stsd[0], min(stsd[0] + 16, stsd[1]), limit=16) if stsd else None
                # version/flags (4), entry count (4), then the first entry's size (4) and type (4).
                if not head or len(head) < 16 or struct.unpack(">I", head[4:8])[0] == 0:
                    continue
                kind = head[12:16]
                if all(0x20 <= b < 0x7F for b in kind) and kind.strip():
                    return kind.decode("ascii").strip()
    except Exception:
        return None
    return None
