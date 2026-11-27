"""Notes in a persona pack.

- ``notes/*.txt`` (optional): one note per file. Headers, a blank line, then the text,
  whose first line is the title. ``- item`` lines become a dashed list and
  ``[ ] item`` / ``[x] item`` a checklist:

      folder: Trips
      created: 1905-03-02 09:12
      modified: 1905-03-10 18:40
      pinned: yes
      shared: besso, mileva

      Packing list
      [x] Violin
      [ ] Patent drafts

  ``shared:`` lists the contacts the note is shared with over iCloud, by id; it's
  shared with each by their first email address, else their first phone number.
  ``deleted: 1905-04-01 08:00`` deletes the note then: a backup in the 30 days after
  has it in Recently Deleted, a later one doesn't have it at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backupgen.persona.core import AreaData, Contact, Pack, PersonaError

_NOTE_HEADERS = ("folder", "created", "modified", "pinned", "shared", "deleted")


@dataclass
class Note:
    id: str
    folder: str
    created: datetime  # real wall-clock times, like messages
    modified: datetime
    pinned: bool
    text: str  # as typed: the first line is the title, "- " and "[ ] " mark list items
    where: str
    shared_with: tuple[str, ...] = ()  # contact ids it's shared with over iCloud
    deleted: datetime | None = None  # when it went to Recently Deleted, a real wall-clock time

    @property
    def real_time(self) -> datetime:
        return self.created


def _parse_note(path: Path, contacts: dict[str, Contact]) -> Note:
    head, _, body = path.read_text(encoding="utf-8").partition("\n\n")
    headers = {}
    for number, line in enumerate(head.splitlines(), 1):
        key, _, value = line.partition(":")
        if key.strip() not in _NOTE_HEADERS or not value.strip():
            raise PersonaError(f"{path.name}:{number}: expected one of {', '.join(_NOTE_HEADERS)}, got {line!r}")
        headers[key.strip()] = value.strip()
    if "created" not in headers:
        raise PersonaError(f"{path.name}: needs a created: header")
    text = body.strip("\n")
    if not text:
        raise PersonaError(f"{path.name}: the note is empty")
    created = datetime.fromisoformat(headers["created"])
    modified = datetime.fromisoformat(headers.get("modified", headers["created"]))
    if modified < created:
        raise PersonaError(f"{path.name}: modified before it was created")
    deleted = datetime.fromisoformat(headers["deleted"]) if "deleted" in headers else None
    if deleted and deleted < modified:
        raise PersonaError(f"{path.name}: deleted before it was last modified")
    shared = tuple(c.strip() for c in headers.get("shared", "").split(",") if c.strip())
    for who in shared:
        if who not in contacts:
            raise PersonaError(f"{path.name}: shared with {who!r}, who isn't in contacts")
        if not contacts[who].emails and not contacts[who].phones:
            raise PersonaError(f"{path.name}: shared with {who!r}, who has no email address or phone number")
    return Note(path.stem, headers.get("folder", "Notes"), created, modified,
                headers.get("pinned", "no").lower() in ("yes", "true"), text, path.name, shared, deleted)


def load(pack: Pack) -> AreaData:
    """Persona.notes: the notes, or None when the pack has no notes/ folder."""
    if not (pack.folder / "notes").is_dir():
        return AreaData({"notes": None})
    notes = [_parse_note(p, pack.contacts) for p in sorted((pack.folder / "notes").glob("*.txt"))]
    return AreaData({"notes": notes}, records=notes)
