"""The camera roll and its People album in a persona pack.

- ``photos.json`` (optional): the camera roll, ``{"photos": [...]}``. Each photo has an
  ``id``, ``taken`` (local time, ``YYYY-MM-DDTHH:MM``), optional ``lat``/``lon``, ``place``,
  ``favorite``, ``trashed`` and ``albums`` (album titles). The pixels come either from
  ``url`` (an openly licensed original, checked against its ``sha1`` and cached; record
  ``licence``, ``credit`` and ``commons_page`` with it; ``thumb_url``/``thumb_sha1`` name a
  smaller rendition to fetch first) or from ``placeholder``
  (``{"size": [640, 480], "color": "#88aacc"}``, drawn locally, for tests).
  Optional ``people`` are the groups the Photos app made of the faces it found (its People
  album): each has an ``id``, optional ``name`` (else unnamed, as most groups are; a
  ``contact`` id links the group to that card and names it after it unless ``name`` says
  otherwise), ``merged_into`` (another person's id: a group the user merged into theirs),
  ``pet`` (``dog`` or ``cat``) and ``key`` (the photo id of the face Photos shows for them;
  else their first). A photo's ``faces`` list
  ``{"person": id, "x": 0.4, "y": 0.45, "size": 0.18}``: the face box's centre and size as
  fractions of the photo, plus ``"hidden": true`` for a face the user hid from People.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from backupgen.persona.core import AreaData, Contact, Pack, PersonaError


@dataclass
class Photo:
    id: str
    real_time: datetime  # when it was taken, local wall-clock time
    lat: Optional[float]
    lon: Optional[float]
    place: str
    favorite: bool
    trashed: bool
    albums: list[str]
    url: str  # an openly licensed original, or "" for a placeholder
    sha1: str
    placeholder: Optional[tuple[int, int, str]]  # (width, height, "#rrggbb")
    where: str
    licence: str = ""
    credit: str = ""
    source: str = ""  # the original's page, e.g. on Wikimedia Commons
    thumb_url: str = ""  # a smaller rendition of the original, tried first
    thumb_sha1: str = ""
    faces: list[Face] = field(default_factory=list)


@dataclass
class Face:
    """A face the Photos app found in a photo: its box's centre and size as fractions of the photo."""

    person: str  # a PhotoPerson id
    x: float
    y: float
    size: float
    hidden: bool = False  # hidden from People by the user


@dataclass
class PhotoPerson:
    """A group the Photos app made of one person's (or pet's) faces: a row of its People album."""

    id: str
    name: Optional[str]  # None: never named, like most groups
    contact: Optional[str]  # a contact id the user linked the group to
    merged_into: Optional[str]  # another PhotoPerson id
    pet: Optional[str]  # "dog" or "cat"
    key: Optional[str]  # the photo id of the face Photos shows for them


def _parse_photos(path: Path) -> list[Photo]:
    photos, seen = [], set()
    for n, raw in enumerate(json.loads(path.read_text(encoding="utf-8"))["photos"], 1):
        where = f"{path.name} photo {n} ({raw.get('id', '?')})"
        if raw["id"] in seen:
            raise PersonaError(f"{where}: duplicate id")
        seen.add(raw["id"])
        placeholder = raw.get("placeholder")
        if bool(placeholder) == bool(raw.get("url")):
            raise PersonaError(f"{where}: needs either url (with sha1) or placeholder")
        if raw.get("url") and not raw.get("sha1"):
            raise PersonaError(f"{where}: a url needs the original's sha1")
        if ("lat" in raw) != ("lon" in raw):
            raise PersonaError(f"{where}: lat and lon go together")
        photos.append(Photo(
            raw["id"], datetime.fromisoformat(raw["taken"]), raw.get("lat"), raw.get("lon"), raw.get("place", ""),
            bool(raw.get("favorite")), bool(raw.get("trashed")), list(raw.get("albums", [])),
            raw.get("url", ""), raw.get("sha1", ""),
            (*placeholder["size"], placeholder["color"]) if placeholder else None, where,
            raw.get("licence", ""), raw.get("credit", ""), raw.get("commons_page", ""),
            raw.get("thumb_url", ""), raw.get("thumb_sha1", ""),
            [_parse_face(face, f"{where} face {m}") for m, face in enumerate(raw.get("faces", []), 1)],
        ))
    return photos


def _parse_face(raw: dict, where: str) -> Face:
    try:
        face = Face(str(raw["person"]), float(raw["x"]), float(raw["y"]), float(raw["size"]), bool(raw.get("hidden")))
    except (KeyError, TypeError, ValueError):
        raise PersonaError(f"{where}: a face needs person, x, y and size") from None
    if not (0 <= face.x <= 1 and 0 <= face.y <= 1 and 0 < face.size <= 1):
        raise PersonaError(f"{where}: x and y go from 0 to 1, size from above 0 to 1")
    return face


_PETS = ("dog", "cat")


def _parse_photo_people(path: Path, photos: list[Photo], contacts: dict[str, Contact]) -> list[PhotoPerson]:
    people: list[PhotoPerson] = []
    for n, raw in enumerate(json.loads(path.read_text(encoding="utf-8")).get("people", []), 1):
        where = f"{path.name} person {n} ({raw.get('id', '?')})"
        if not raw.get("id") or any(p.id == raw["id"] for p in people):
            raise PersonaError(f"{where}: needs an id of its own")
        if raw.get("contact") and raw["contact"] not in contacts:
            raise PersonaError(f"{where}: no contact {raw['contact']!r}")
        if raw.get("pet") and raw["pet"] not in _PETS:
            raise PersonaError(f"{where}: pet is dog or cat")
        people.append(PhotoPerson(raw["id"], raw.get("name"), raw.get("contact"), raw.get("merged_into"),
                                  raw.get("pet"), raw.get("key")))
    ids = {p.id for p in people}
    for person in people:
        if person.merged_into is not None and (person.merged_into not in ids or person.merged_into == person.id):
            raise PersonaError(f"{path.name}: {person.id} is merged into {person.merged_into!r}, which isn't another person")
        if person.key and not any(f.person == person.id for p in photos if p.id == person.key for f in p.faces):
            raise PersonaError(f"{path.name}: {person.id}'s key photo {person.key!r} has no face of theirs")
    for photo in photos:
        for face in photo.faces:
            if face.person not in ids:
                raise PersonaError(f"{photo.where}: a face of {face.person!r}, who isn't in people")
    return people


def load(pack: Pack) -> AreaData:
    """Persona.photos (None when the pack has no photos.json) and Persona.photo_people, the People album's groups."""
    path = pack.folder / "photos.json"
    photos = _parse_photos(path) if path.exists() else None
    people = _parse_photo_people(path, photos, pack.contacts) if photos is not None else []
    return AreaData({"photos": photos, "photo_people": people}, records=photos or [])
