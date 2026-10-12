"""Pixels for generated photos: fetched originals or drawn placeholders, saved as camera JPEGs.

Originals are openly licensed images listed in a persona's photos.json by URL and SHA-1.
They're downloaded once into a cache ($BACKUPGEN_MEDIA_CACHE, or ~/.cache/backupgen/media)
and checked against the SHA-1, so a changed or tampered file is never used. Originals can
run to tens of megabytes, so a photo can also name a smaller rendition (``thumb_url`` and
``thumb_sha1``, e.g. Wikimedia's 1920 px thumbnail); if that has changed, the original is
used instead. Each is
then scaled down and saved the way an iPhone camera would label it: Apple make and
model, the time taken with its UTC offset, and GPS coordinates.
"""

from __future__ import annotations

import hashlib
import io
import os
import random
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
