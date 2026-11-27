"""Contacts in a persona pack: the cards in the phone's address book, the owner's own among them.

- ``persona.json``'s ``contacts``: a list of cards. ``owner`` names the owner's card by id.
- ``contacts.json`` (optional): more cards, ``{"contacts": [...]}``, read after persona.json's
  in the order written. It's the Contacts data type's own file, so cards can be added and
  changed without touching persona.json.

A card:

    {"id": "mileva", "first": "Mileva", "last": "Marić", "nickname": "Dollie",
     "phones": [["mobile", "+41 31 555 0101"]], "emails": [["home", "mileva@example.org"]],
     "added": "1903-01-06"}

``id`` is how the rest of the pack names the person, and no two cards share one. Also
``organization``, ``department``, ``job_title`` and ``note``; phone and email labels are
mobile, home, work, main, other and iphone. ``handle`` is their Messages address (default:
the first phone as E.164, else the first email). ``added`` is the real date the card was
created: a backup made before it doesn't have the card (default: the start of the period).

A card is Contacts data: adding one changes AddressBook.sqlitedb and the contacts answer key,
however it's used. Messages chats, shared notes, calendar people and the People album name
people by card id, so a person there needs a card. Call history and voicemail can name someone
without one by an E.164 number (``+447700900099``), and WhatsApp's people.txt adds people who
aren't contacts (whatsapp.py), which leaves Contacts as it was.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional


@dataclass
class Contact:
    id: str
    first: str
    last: str = ""
    nickname: str = ""
    organization: str = ""
    department: str = ""
    job_title: str = ""
    note: str = ""
    phones: list[tuple[str, str]] = field(default_factory=list)  # (label, number as displayed)
    emails: list[tuple[str, str]] = field(default_factory=list)
    handle: str = ""  # Messages handle: E.164 phone or email
    added: Optional[date] = None  # real date the card was created

    @property
    def display_name(self) -> str:
        return " ".join(p for p in (self.first, self.last) if p) or self.organization


def _contact(raw: dict) -> Contact:
    return Contact(
        id=raw["id"],
        first=raw.get("first", ""),
        last=raw.get("last", ""),
        nickname=raw.get("nickname", ""),
        organization=raw.get("organization", ""),
        department=raw.get("department", ""),
        job_title=raw.get("job_title", ""),
        note=raw.get("note", ""),
        phones=[tuple(p) for p in raw.get("phones", [])],
        emails=[tuple(e) for e in raw.get("emails", [])],
        handle=raw.get("handle") or _default_handle(raw),
        added=date.fromisoformat(raw["added"]) if raw.get("added") else None,
    )


def _default_handle(raw: dict) -> str:
    if raw.get("phones"):
        return "+" + re.sub(r"\D", "", raw["phones"][0][1])
    if raw.get("emails"):
        return raw["emails"][0][1].lower()
    return ""


def load_cards(folder: Path, persona_json: dict) -> dict[str, Contact]:
    """Every card, the owner's included, by id: persona.json's, then contacts.json's."""
    # core imports this module for Contact, so its helpers are imported here rather than at the top.
    from backupgen.persona.core import PersonaError, _read_json

    sources = [("persona.json", persona_json.get("contacts", []))]
    path = folder / "contacts.json"
    if path.exists():
        raw = _read_json(path)
        if not isinstance(raw, dict) or not isinstance(raw.get("contacts"), list):
            raise PersonaError(f'{path.name}: expected {{"contacts": [...]}}')
        sources.append((path.name, raw["contacts"]))
    cards: dict[str, Contact] = {}
    for name, entries in sources:
        for n, card in enumerate(entries, 1):
            if not card.get("id"):
                raise PersonaError(f'{name} contact {n}: needs an "id"')
            if card["id"] in cards:
                raise PersonaError(f"{name} contact {n} ({card['id']}): duplicate id")
            cards[card["id"]] = _contact(card)
    return cards
