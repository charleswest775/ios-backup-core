"""WhatsApp in a persona pack.

- ``whatsapp/*.txt`` (optional): WhatsApp chats, written like ``chats/*.txt``:

      chat: uo-friends
      type: group               individual (the default), group, broadcast or status
      with: hana, claire
      name: MLA '18             groups and broadcast lists
      created_by: hana          groups: who made it (default me)
      addressing: lid           groups: members by LID, as newer groups are (WhatsApp 2.23 on)

      == 2024-07-08
      19:00 notice: encrypted   "Messages and calls are end-to-end encrypted."
      19:00 notice: created     groups: the group was made (by created_by)
      19:00 notice: added me    groups: someone was added ("added hana" for others)
      19:02 hana: Hello
      19:03 me > hana: A reply to Hana's latest message
      19:03 claire +❤️          a reaction (any emoji) on the latest message from someone else
      19:04 me [photo:rainier-01] An optional caption
      19:04 hana [photo:missing] A photo WhatsApp no longer kept: only its thumbnail is left
      19:05 hana [voice:12s]
      19:06 me [document:Plan.pdf]          its text comes from whatsapp/documents/Plan.pdf.txt
      19:07 me [location:45.52,-122.68 Place name]
      19:08 me [live-location:45.52,-122.68]
      19:09 hana [call:video 4m12s]         voice or video; a length, missed (incoming) or unanswered (outgoing)
      19:10 hana [deleted]                  deleted for everyone
      19:11* hana: Starred                  a * after the time stars the message

  ``whatsapp/people.txt`` lists what people call themselves on WhatsApp, and who's on it
  without being a contact: ``lucia - Lucía 🌵`` (a contact: the number is their first phone)
  or ``dani +1 760 555 0150 Dani Reyes`` (not in the address book: id, number, name).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from backupgen.persona.core import Contact, PersonaError, _seconds

_WA_LINE = re.compile(
    r"^(\d{1,2}):(\d{2})(\*)?\s+([A-Za-z0-9_-]+)(?:\s+>\s+([A-Za-z0-9_-]+))?"
    r"(?::\s(.*)|\s+\+(\S+)|\s+\[([a-z-]+)(?::([^\]]*))?\](?:\s+(.*))?)\s*$"
)
_WA_HEADERS = ("chat", "type", "with", "name", "created_by", "addressing")
_WA_TYPES = ("individual", "group", "broadcast", "status")
_WA_PERSON = re.compile(r"^([A-Za-z0-9_-]+)\s+(-|\+\d[\d ]*\d)\s+([^\d\s].*)$")
WA_MISSING_PHOTO = "missing"
_WA_ANSWERABLE = ("text", "photo", "voice", "document", "location", "live_location")  # what replies and reactions point at


@dataclass
class WAPerson:
    """Someone on WhatsApp: a contact, or a number that isn't in the address book."""

    id: str
    number: str  # E.164, "+15035550102"
    push_name: str  # the name they gave WhatsApp
    contact: Optional[Contact] = None


@dataclass
class WAMessage:
    real_time: datetime
    sender: str  # "me", a person id, or "notice"
    kind: str  # text, photo, voice, document, location, live_location, call, deleted, notice, reaction
    where: str
    text: str = ""  # the text, or a photo's caption
    starred: bool = False
    reply_to: Optional[str] = None  # whose latest message this answers ("me" or a person id)
    reaction: str = ""  # the emoji
    photo: str = ""  # a photos.json id, or WA_MISSING_PHOTO
    duration: int = 0  # voice notes and calls, seconds
    document: str = ""  # file name
    lat: Optional[float] = None
    lon: Optional[float] = None
    place: str = ""
    video: bool = False  # calls
    outcome: str = ""  # calls: answered, missed or unanswered
    notice: str = ""  # encrypted, created or added
    member: str = ""  # who an "added" notice is about: "me" or a person id


@dataclass
class WAChat:
    id: str
    type: str  # individual, group, broadcast or status
    participants: list[str]  # person ids (a broadcast list's recipients)
    name: str
    creator: str  # groups: "me" or a person id
    lid: bool  # groups: members addressed by LID
    messages: list[WAMessage]


def _parse_wa_people(folder: Path, contacts: dict[str, Contact]) -> dict[str, WAPerson]:
    """Everyone with a phone number can be on WhatsApp; people.txt adds names and non-contacts."""
    people = {}
    for cid, contact in contacts.items():
        if contact.phones:
            number = "+" + re.sub(r"\D", "", contact.phones[0][1])
            people[cid] = WAPerson(cid, number, contact.first or contact.display_name, contact)
    path = folder / "people.txt"
    if not path.exists():
        return people
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line, where = raw.strip(), f"whatsapp/people.txt:{n}"
        if not line or line.startswith("#"):
            continue
        match = _WA_PERSON.match(line)
        if not match:
            raise PersonaError(f"{where}: can't parse {line!r} (write: id - Name, or id +1 555 0100 Name)")
        pid, number, name = match.group(1), match.group(2).strip(), match.group(3).strip()
        if number == "-":
            if pid not in people:
                raise PersonaError(f"{where}: {pid!r} isn't a contact with a phone number")
            people[pid].push_name = name
        else:
            if pid in contacts:
                raise PersonaError(f"{where}: {pid!r} is a contact; write '{pid} - {name}'")
            people[pid] = WAPerson(pid, "+" + re.sub(r"\D", "", number), name)
    return people


def _wa_tag(tag: str, arg: str, where: str) -> dict:
    """The fields of a bracketed WhatsApp message, e.g. ``[call:video 4m12s]``."""
    arg = (arg or "").strip()
    if tag == "photo":
        if not arg:
            raise PersonaError(f"{where}: [photo:<id>] needs a photo id")
        return {"kind": "photo", "photo": arg}
    if tag == "voice":
        return {"kind": "voice", "duration": _seconds(arg, where)}
    if tag == "document":
        if not arg:
            raise PersonaError(f"{where}: [document:<file name>] needs a file name")
        return {"kind": "document", "document": arg}
    if tag in ("location", "live-location"):
        match = re.match(r"^(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)(?:\s+(.+))?$", arg)
        if not match:
            raise PersonaError(f"{where}: write [{tag}:<lat>,<lon> <place>]")
        lat, lon = float(match.group(1)), float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise PersonaError(f"{where}: {lat},{lon} isn't a place on Earth")
        kind = "live_location" if tag == "live-location" else "location"
        return {"kind": kind, "lat": lat, "lon": lon, "place": (match.group(3) or "").strip()}
    if tag == "call":
        match = re.match(r"^(voice|video)\s+(missed|unanswered|[\dhms]+)$", arg)
        if not match:
            raise PersonaError(f"{where}: write [call:voice 4m12s], [call:video missed] or [call:voice unanswered]")
        medium, length = match.groups()
        if length in ("missed", "unanswered"):
            return {"kind": "call", "video": medium == "video", "outcome": length}
        return {"kind": "call", "video": medium == "video", "outcome": "answered", "duration": _seconds(length, where)}
    if tag == "deleted":
        if arg:
            raise PersonaError(f"{where}: [deleted] takes nothing else")
        return {"kind": "deleted"}
    raise PersonaError(f"{where}: unknown [{tag}]")


def _parse_wa_chat(path: Path, people: dict[str, WAPerson]) -> WAChat:
    headers: dict[str, str] = {}
    messages: list[WAMessage] = []
    day: Optional[date] = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.rstrip()
        where = f"whatsapp/{path.name}:{number}"
        if not line or line.startswith("#"):
            continue
        if line.startswith("=="):
            day = date.fromisoformat(line[2:].strip())
            continue
        header = re.match(rf"^({'|'.join(_WA_HEADERS)}):\s*(.*)$", line)
        if header and day is None:
            headers[header.group(1)] = header.group(2).strip()
            continue
        match = _WA_LINE.match(line)
        if not match or day is None:
            raise PersonaError(f"{where}: can't parse {line!r}")
        hour, minute, star, sender, reply_to, text, reaction, tag, arg, caption = match.groups()
        when = datetime(day.year, day.month, day.day, int(hour), int(minute))
        fields: dict = {}
        if sender == "notice":
            words = (text or "").split()
            if tag or reaction or reply_to or star or not words:
                raise PersonaError(f"{where}: write 'notice: encrypted', 'notice: created' or 'notice: added <who>'")
            if words == ["encrypted"] or words == ["created"]:
                fields = {"kind": "notice", "notice": words[0]}
            elif len(words) == 2 and words[0] == "added":
                fields = {"kind": "notice", "notice": "added", "member": words[1]}
            else:
                raise PersonaError(f"{where}: unknown notice {text!r}")
        elif reaction:
            if reply_to or star:
                raise PersonaError(f"{where}: a reaction can't reply or be starred")
            fields = {"kind": "reaction", "reaction": reaction}
        elif tag:
            fields = _wa_tag(tag, arg, where)
            if caption:
                if fields["kind"] != "photo":
                    raise PersonaError(f"{where}: only photos take a caption")
                fields["text"] = caption.strip()
            if reply_to:
                raise PersonaError(f"{where}: only text messages can reply")
        else:
            fields = {"kind": "text", "text": text}
        messages.append(WAMessage(when, sender, where=where, starred=bool(star), reply_to=reply_to, **fields))

    chat_type = headers.get("type", "individual")
    if chat_type not in _WA_TYPES:
        raise PersonaError(f"whatsapp/{path.name}: type must be one of {', '.join(_WA_TYPES)}")
    participants = [p.strip() for p in headers.get("with", "").split(",") if p.strip()]
    for p in participants:
        if p not in people:
            raise PersonaError(f"whatsapp/{path.name}: {p!r} isn't a contact with a phone or in whatsapp/people.txt")
    if not participants:
        raise PersonaError(f"whatsapp/{path.name}: needs a with: line")
    if chat_type == "individual" and len(participants) != 1:
        raise PersonaError(f"whatsapp/{path.name}: a one-to-one chat has one other person (type: group for more)")
    if chat_type in ("group", "broadcast") and not headers.get("name"):
        raise PersonaError(f"whatsapp/{path.name}: a {chat_type} needs a name: line")
    creator = headers.get("created_by", "me")
    if creator != "me" and creator not in participants:
        raise PersonaError(f"whatsapp/{path.name}: created_by {creator!r} isn't in the group")
    chat = WAChat(headers.get("chat", path.stem), chat_type, participants, headers.get("name", ""), creator,
                  headers.get("addressing", "phone") == "lid", messages)

    previous = None
    seen: list[WAMessage] = []
    for m in messages:
        if previous and m.real_time < previous:
            raise PersonaError(f"{m.where}: messages must be in time order")
        previous = m.real_time
        if m.sender not in ("me", "notice") and m.sender not in participants:
            raise PersonaError(f"{m.where}: {m.sender!r} isn't in this chat")
        if chat_type == "broadcast" and m.sender != "me":
            raise PersonaError(f"{m.where}: only you write in a broadcast list")
        if chat_type == "status" and m.sender in ("me", "notice"):
            raise PersonaError(f"{m.where}: status updates here are other people's")
        if chat_type == "status" and m.kind != "text":
            raise PersonaError(f"{m.where}: status updates here are text")
        if m.kind == "notice" and m.notice in ("created", "added") and chat_type != "group":
            raise PersonaError(f"{m.where}: only groups have {m.notice} notices")
        if m.kind == "notice" and m.notice == "added" and m.member != "me" and m.member not in participants:
            raise PersonaError(f"{m.where}: {m.member!r} isn't in this group")
        if m.kind == "call" and chat_type != "individual":
            raise PersonaError(f"{m.where}: calls are written in one-to-one chats")
        if m.kind == "call" and m.outcome == "missed" and m.sender == "me":
            raise PersonaError(f"{m.where}: 'missed' is for calls you didn't pick up; yours are 'unanswered'")
        if m.kind == "call" and m.outcome == "unanswered" and m.sender != "me":
            raise PersonaError(f"{m.where}: 'unanswered' is for your calls; theirs are 'missed'")
        if m.reply_to:
            if m.reply_to not in ("me", *participants):
                raise PersonaError(f"{m.where}: {m.reply_to!r} isn't in this chat")
            if not any(t.sender == m.reply_to and t.kind in _WA_ANSWERABLE for t in seen):
                raise PersonaError(f"{m.where}: no earlier message from {m.reply_to} to reply to")
        if m.kind == "reaction" and not any(t.sender != m.sender and t.kind in _WA_ANSWERABLE for t in seen):
            raise PersonaError(f"{m.where}: a reaction with nothing to react to")
        seen.append(m)
    return chat


def _parse_whatsapp(folder: Path, contacts: dict[str, Contact]):
    people = _parse_wa_people(folder, contacts)
    chats = [_parse_wa_chat(p, people) for p in sorted(folder.glob("*.txt")) if p.name != "people.txt"]
    ids = [c.id for c in chats]
    if len(ids) != len(set(ids)):
        raise PersonaError("whatsapp: duplicate chat ids")
    documents = {p.name.removesuffix(".txt"): p.read_text(encoding="utf-8").strip("\n")
                 for p in sorted((folder / "documents").glob("*.txt"))} if (folder / "documents").is_dir() else {}
    for chat in chats:
        for m in chat.messages:
            if m.kind == "document" and m.document not in documents:
                raise PersonaError(f"{m.where}: no whatsapp/documents/{m.document}.txt")
    return chats, people, documents


def load_whatsapp(folder: Path, contacts: dict[str, Contact]
                  ) -> tuple[Optional[list[WAChat]], dict[str, WAPerson], dict[str, str]]:
    """(chats, people, documents), or (None, {}, {}) when the pack has no whatsapp/ folder."""
    if not (folder / "whatsapp").is_dir():
        return None, {}, {}
    return _parse_whatsapp(folder / "whatsapp", contacts)
