"""Media for generated backups: photos (fetched originals or drawn placeholders, saved as camera
JPEGs) and silent Voice Memos recordings (``silent_m4a``).

Originals are openly licensed images listed in a persona's photos.json by URL and SHA-1.
They're downloaded once into a cache ($BACKUPGEN_MEDIA_CACHE, or ~/.cache/backupgen/media)
and checked against the SHA-1, so a changed or tampered file is never used. Originals can
run to tens of megabytes, so a photo can also name a smaller rendition (``thumb_url`` and
``thumb_sha1``, e.g. Wikimedia's 1920 px thumbnail); if that has changed, the original is
used instead. Each is
then scaled down and saved the way an iPhone camera would label it: Apple make and
model, the time taken with its UTC offset, and GPS coordinates.

Recordings are AAC-LC .m4a files of digital silence, written with the standard library and
laid out the way Voice Memos writes them: the title, encoder and the recording's unique id in
``moov/udta/meta/ilst`` (``©nam``, ``©too`` and the freeform ``com.apple.iTunes:voice-memo-uuid``),
and, for a transcribed recording (iOS 18 on), the transcript as raw JSON in the audio track's
``moov/trak/udta/tsrp`` box. As Apple's AAC encoder does, the audio starts with 2112 samples
of encoder delay that an edit list (``edts/elst``) skips, so players report the length asked
for, to the sample. Checked with ``afinfo``, ``ffprobe`` and an ``ffmpeg`` decode.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import struct
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

USER_AGENT = "backupgen/0.1 (synthetic iPhone backups for testing; https://github.com/charleswest775/ios-backup-core)"
LONG_EDGE = 1600  # big enough for screenshots, small enough to keep backups to tens of MB


class MediaError(RuntimeError):
    pass


def cache_dir() -> Path:
    return Path(os.environ.get("BACKUPGEN_MEDIA_CACHE") or Path.home() / ".cache" / "backupgen" / "media")


def fetch(url: str, sha1: str) -> bytes:
    """The original at ``url``, from the cache or downloaded, checked against ``sha1``."""
    path = cache_dir() / sha1
    if path.is_file():
        data = path.read_bytes()
        if hashlib.sha1(data).hexdigest() == sha1:
            return data
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 5:
                raise MediaError(f"couldn't download {url}: {e}") from e
            # Wikimedia limits how fast one client may download; wait as long as it asks.
            time.sleep(int(e.headers.get("Retry-After") or 20 * (attempt + 1)))
        except OSError as e:
            raise MediaError(f"couldn't download {url}: {e}") from e
    if hashlib.sha1(data).hexdigest() != sha1:
        raise MediaError(f"{url} doesn't match its SHA-1 {sha1}; the file has changed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def placeholder(width: int, height: int, color: str, rng: random.Random) -> Image.Image:
    """A flat-coloured frame with a few bands, so placeholders don't all look alike."""
    image = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(image)
    base = Image.new("RGB", (1, 1), color).getpixel((0, 0))
    for _ in range(4):
        top = rng.randrange(height)
        shade = tuple(max(0, min(255, c + rng.randrange(-40, 41))) for c in base)
        draw.rectangle([0, top, width, top + rng.randrange(height // 8 + 1, height // 3 + 2)], fill=shade)
    return image


class Renderer:
    """Each persona photo as a camera JPEG, rendered once for the camera roll and for Messages."""

    def __init__(self, persona, snapshot):
        self.persona, self.snapshot = persona, snapshot
        self.photos = {photo.id: photo for photo in persona.photos or []}
        self._done: dict[str, tuple[bytes, int, int]] = {}

    def jpeg(self, photo_id: str) -> tuple[bytes, int, int]:
        if photo_id not in self._done:
            photo = self.photos[photo_id]
            if photo.placeholder:
                width, height, color = photo.placeholder
                # Seeded per photo, so a placeholder looks the same whichever writer draws it first.
                image = placeholder(width, height, color, random.Random(f"{self.persona.id}/{photo.id}"))
            else:
                try:
                    if not photo.thumb_url:
                        raise MediaError("no smaller rendition")
                    data = fetch(photo.thumb_url, photo.thumb_sha1)
                except MediaError:
                    data = fetch(photo.url, photo.sha1)
                image = Image.open(io.BytesIO(data))
            taken = self.persona.device_time(photo.real_time).astimezone(self.persona.tz)
            self._done[photo_id] = camera_jpeg(image, taken, self.snapshot.product_name, self.snapshot.ios,
                                               photo.lat, photo.lon)
        return self._done[photo_id]


def _dms(value: float) -> tuple[float, float, float]:
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = round(((value - degrees) * 60 - minutes) * 60, 2)
    return float(degrees), float(minutes), seconds


def camera_jpeg(image: Image.Image, taken: datetime, model: str, software: str,
                lat: Optional[float], lon: Optional[float], long_edge: int = LONG_EDGE) -> tuple[bytes, int, int]:
    """JPEG bytes labelled like an iPhone photo, and its width and height. ``taken`` is aware."""
    image = image.convert("RGB")
    if max(image.size) > long_edge:
        image.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    stamp = taken.strftime("%Y:%m:%d %H:%M:%S")
    offset = taken.strftime("%z")
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[0x0110] = model
    exif[0x0131] = software
    exif[0x0132] = stamp
    detail = exif.get_ifd(0x8769)
    detail[0x9003] = stamp  # DateTimeOriginal
    detail[0x9004] = stamp  # DateTimeDigitized
    detail[0x9011] = f"{offset[:3]}:{offset[3:]}"  # OffsetTimeOriginal
    if lat is not None and lon is not None:
        gps = exif.get_ifd(0x8825)
        gps[1], gps[2] = ("N" if lat >= 0 else "S"), _dms(lat)
        gps[3], gps[4] = ("E" if lon >= 0 else "W"), _dms(lon)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85, exif=exif)
    return out.getvalue(), image.width, image.height


# ── silent Voice Memos recordings ────────────────────────────────────────────

AAC_RATE = 48000
SILENT_AAC_FRAME = bytes.fromhex("01182007")  # one 1024-sample AAC-LC mono frame of digital silence (ffmpeg's)
_MP4_EPOCH = 2_082_844_800  # MP4 times count seconds from 1904-01-01; this is 1970-01-01
_AAC_PRIMING = 2112  # samples of encoder delay at the start of an AAC stream, skipped by the edit list


def _box(kind: bytes, *payload: bytes) -> bytes:
    body = b"".join(payload)
    return struct.pack(">I", 8 + len(body)) + kind + body


def _full(kind: bytes, version: int, flags: int, *payload: bytes) -> bytes:
    return _box(kind, struct.pack(">I", (version << 24) | flags), *payload)


def _descr(tag: int, body: bytes) -> bytes:
    n = len(body)  # the 4-byte length form, as Apple writes it
    return bytes([tag, 0x80 | (n >> 21) & 0x7F, 0x80 | (n >> 14) & 0x7F, 0x80 | (n >> 7) & 0x7F, n & 0x7F]) + body


def _m4a_samples(seconds: float) -> int:
    return max(1, round(seconds * AAC_RATE))


def m4a_duration(seconds: float) -> float:
    """How long ``silent_m4a(seconds)`` plays, in seconds: ``seconds`` to the nearest sample."""
    return _m4a_samples(seconds) / AAC_RATE


def transcript_json(segments: list, locale: str = "en_US") -> bytes:
    """A Voice Memos transcript as its ``tsrp`` box holds it (shape A: runs plus an attribute table).

    ``segments`` is ``[(text, start, end), ...]``; the text is the runs joined, so words after the
    first carry their leading space.
    """
    runs: list = []
    table = []
    for n, (text, start, end) in enumerate(segments):
        runs += [text, n]
        table.append({"timeRange": [round(start, 2), round(end, 2)]})
    doc = {"attributedString": {"attributeTable": table, "runs": runs}, "locale": {"identifier": locale, "current": 0}}
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _ilst_item(kind: bytes, value: str) -> bytes:
    return _box(kind, _box(b"data", struct.pack(">II", 1, 0), value.encode("utf-8")))  # type 1: UTF-8 text


def _freeform(name: str, value: str) -> bytes:
    return _box(b"----", _full(b"mean", 0, 0, b"com.apple.iTunes"), _full(b"name", 0, 0, name.encode("utf-8")),
                _box(b"data", struct.pack(">II", 1, 0), value.encode("utf-8")))


def silent_m4a(seconds: float, created_unix: int = 0, *, title: Optional[str] = None, encoder: Optional[str] = None,
               unique_id: Optional[str] = None, transcript: Optional[list] = None, locale: str = "en_US") -> bytes:
    """A silent AAC-LC .m4a about ``seconds`` long, tagged like a Voice Memos recording.

    ``created_unix`` dates the movie and track headers. ``title``, ``encoder`` and ``unique_id``
    become the ``©nam``, ``©too`` and ``voice-memo-uuid`` tags; ``transcript``
    (``[(text, start, end), ...]``) is stored in the track's ``udta/tsrp`` box.
    """
    valid = _m4a_samples(seconds)
    frames = -(-(valid + _AAC_PRIMING) // 1024)
    samples = frames * 1024
    stamp = int(created_unix) + _MP4_EPOCH
    asc = bytes([0x11, 0x88])  # AudioSpecificConfig: AAC-LC (2), 48 kHz (index 3), mono (1)
    decoder = _descr(0x04, bytes([0x40, 0x15]) + bytes(3) + struct.pack(">II", 64000, 64000) + _descr(0x05, asc))
    esds = _full(b"esds", 0, 0, _descr(0x03, struct.pack(">HB", 1, 0) + decoder + _descr(0x06, b"\x02")))
    mp4a = _box(b"mp4a", bytes(6), struct.pack(">H", 1), bytes(8), struct.pack(">HHHH", 1, 16, 0, 0),
                struct.pack(">I", AAC_RATE << 16), esds)
    stsd = _full(b"stsd", 0, 0, struct.pack(">I", 1), mp4a)
    stts = _full(b"stts", 0, 0, struct.pack(">III", 1, frames, 1024))
    stsc = _full(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, frames, 1))
    stsz = _full(b"stsz", 0, 0, struct.pack(">II", len(SILENT_AAC_FRAME), frames))
    ftyp = _box(b"ftyp", b"M4A ", bytes(4), b"M4A ", b"isom", b"mp42")
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    millis = round(valid * 1000 / AAC_RATE)  # what plays, in the movie's 1/1000 s
    edts = _box(b"edts", _full(b"elst", 0, 0, struct.pack(">I", 1), struct.pack(">IiHH", millis, _AAC_PRIMING, 1, 0)))
    tags = [_ilst_item(b"\xa9nam", title)] if title is not None else []
    if encoder is not None:
        tags.append(_ilst_item(b"\xa9too", encoder))
    if unique_id is not None:
        tags.append(_freeform("voice-memo-uuid", unique_id))
    hdlr_mdir = _full(b"hdlr", 0, 0, bytes(4), b"mdir", b"appl", bytes(8), b"\0")
    movie_udta = _box(b"udta", _full(b"meta", 0, 0, hdlr_mdir, _box(b"ilst", *tags))) if tags else b""
    track_udta = _box(b"udta", _box(b"tsrp", transcript_json(transcript, locale))) if transcript else b""

    def moov(chunk_offset: int) -> bytes:
        stbl = _box(b"stbl", stsd, stts, stsc, stsz, _full(b"stco", 0, 0, struct.pack(">II", 1, chunk_offset)))
        minf = _box(b"minf", _full(b"smhd", 0, 0, bytes(4)),
                    _box(b"dinf", _full(b"dref", 0, 0, struct.pack(">I", 1), _full(b"url ", 0, 1))), stbl)
        mdhd = _full(b"mdhd", 0, 0, struct.pack(">IIIIHH", stamp, stamp, AAC_RATE, samples, 0x55C4, 0))
        hdlr = _full(b"hdlr", 0, 0, bytes(4), b"soun", bytes(12), b"Core Media Audio\0")
        tkhd = _full(b"tkhd", 0, 1, struct.pack(">IIII", stamp, stamp, 1, 0), struct.pack(">I", millis), bytes(8),
                     struct.pack(">hhhH", 0, 0, 0x0100, 0), matrix, bytes(8))
        mvhd = _full(b"mvhd", 0, 0, struct.pack(">IIII", stamp, stamp, 1000, millis), struct.pack(">IH", 0x10000, 0x0100),
                     bytes(10), matrix, bytes(24), struct.pack(">I", 2))
        trak = _box(b"trak", tkhd, edts, _box(b"mdia", mdhd, hdlr, minf), track_udta)
        return _box(b"moov", mvhd, trak, movie_udta)

    offset = len(ftyp) + len(moov(0)) + 8  # the moov is the same size whatever the offset
    return ftyp + moov(offset) + _box(b"mdat", SILENT_AAC_FRAME * frames)
