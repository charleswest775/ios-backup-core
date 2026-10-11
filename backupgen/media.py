"""Pixels for generated photos: fetched originals or drawn placeholders, saved as camera JPEGs.

Originals are openly licensed images listed in a persona's photos.json by URL and SHA-1.
They're downloaded once into a cache ($BACKUPGEN_MEDIA_CACHE, or ~/.cache/backupgen/media)
and checked against the SHA-1, so a changed or tampered file is never used. Each is
then scaled down and saved the way an iPhone camera would label it: Apple make and
model, the time taken with its UTC offset, and GPS coordinates.
"""

from __future__ import annotations

import hashlib
import io
import os
import random
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
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
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
