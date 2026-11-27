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

  ``store: legacy`` puts the note in the old notes.sqlite (iOS 8 and earlier) instead of
  NoteStore: iOS 9 left an account's notes there until the account was upgraded, so a backup
  can hold an "On My iPhone" account there beside iCloud notes (writers/legacy_notes.py).
  Such a note has no folder, pin or sharing; its lines are kept as typed (no lists), so a
  line can't start or end with a space or hold two in a row, and there are never two blank
  lines in a row (HTML can't keep those). ``deleted:`` there sets its ZDELETEDFLAG from that
  date on: it stays in the file, never listed. ``store: notestore`` is the default.

  Two headers are for a note that is only its title. ``data: null``, ``data: empty`` or
  ``data: missing`` make a note whose text can't be read, one for each way a note's data
  can be missing: its ICNoteData row has a NULL ZDATA, an empty one, or there's no row at
  all, so only its title is known and the answer key's text is "". ``snippet: empty``
  writes ZSNIPPET as '' rather than NULL, which a reader must also accept (a note with no
  line after its title has a NULL snippet otherwise).

  Notes waiting for their first download from iCloud (ZNEEDSINITIALFETCHFROMCLOUD 1: an
  ICNote row with only an identifier, an account and empty note data) aren't files here:
  the notes step's ``placeholders`` option says how many (writers/notes.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backupgen.persona.core import AreaData, Contact, Pack, PersonaError

_NOTE_HEADERS = ("folder", "created", "modified", "pinned", "shared", "deleted", "store", "data", "snippet")
STORES = ("notestore", "legacy")
UNREADABLE = ("null", "empty", "missing")  # data: what the note's ICNoteData holds instead of its text


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
    store: str = "notestore"  # "legacy": in notes.sqlite (writers/legacy_notes.py)
    unreadable: str | None = None  # data: "null", "empty" or "missing" ICNoteData; None for a note with its text
    empty_snippet: bool = False  # snippet: empty, ZSNIPPET '' rather than NULL

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
    store = headers.get("store", "notestore")
    if store not in STORES:
        raise PersonaError(f"{path.name}: store: is one of {', '.join(STORES)}, not {store!r}")
    unreadable = headers.get("data")
    if unreadable is not None and unreadable not in UNREADABLE:
        raise PersonaError(f"{path.name}: data: is one of {', '.join(UNREADABLE)}, not {unreadable!r}")
    if headers.get("snippet", "empty") != "empty":
        raise PersonaError(f"{path.name}: snippet: can only be empty, not {headers['snippet']!r}")
    title_only = [h for h in ("data", "snippet") if h in headers]
    if title_only and "\n" in text:
        raise PersonaError(f"{path.name}: a note with {title_only[0]}: is only its title (one line)")
    if store == "legacy":
        _check_legacy(path, headers, text)
    return Note(path.stem, headers.get("folder", "Notes"), created, modified,
                headers.get("pinned", "no").lower() in ("yes", "true"), text, path.name, shared, deleted,
                store, unreadable, "snippet" in headers)


def _check_legacy(path: Path, headers: dict, text: str) -> None:
    """A notes.sqlite note: none of NoteStore's headers, and text its HTML body keeps as typed."""
    for header in ("folder", "pinned", "shared", "data", "snippet"):
        if header in headers:
            raise PersonaError(f"{path.name}: a store: legacy note has no {header}: (notes.sqlite has none)")
    lines = text.split("\n")
    for number, line in enumerate(lines, 1):
        if line != " ".join(line.replace("\xa0", " ").split()):
            raise PersonaError(f"{path.name}: line {number} of a store: legacy note starts or ends with a space, "
                               f"has two in a row or another kind of space, which its HTML body can't keep")
    if "\n\n\n" in text:
        raise PersonaError(f"{path.name}: a store: legacy note can't have two blank lines in a row")


def load(pack: Pack) -> AreaData:
    """Persona.notes: the notes, or None when the pack has no notes/ folder."""
    if not (pack.folder / "notes").is_dir():
        return AreaData({"notes": None})
    notes = [_parse_note(p, pack.contacts) for p in sorted((pack.folder / "notes").glob("*.txt"))]
    return AreaData({"notes": notes}, records=notes)
