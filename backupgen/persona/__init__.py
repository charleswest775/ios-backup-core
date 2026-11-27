"""Persona packs: who the phone belongs to, their contacts, their conversations, their timeline.

A pack is a folder under ``personas/``:

- ``persona.json``: owner, time shift, devices, backup snapshots and cards (contacts.py). The
  pack and each snapshot may also have ``options``: settings for the writers by data type,
  ``{"<data type>": {"<key>": value}}`` (``{}`` when left out), read as ``Persona.options`` and
  ``Snapshot.options``. A snapshot's value for a key overrides the pack's. Each writer declares
  the keys it reads, with their defaults, in its STEP (``Step.options``; the list of every key is
  ``generate.declared_options()``) and reads them merged as ``ctx.options``.
  Generating from a pack whose options name a data type or key no writer declares fails with a
  PersonaError naming it, so a typo can't quietly leave a case out.
- ``contacts.json`` (optional): more cards, after persona.json's ``contacts`` (contacts.py).
  Cards are Contacts data, the contacts lane's: a person a chat, a shared note, an event or the
  People album names needs one, and adding one changes Contacts. Call history and voicemail
  can name someone without a card by an E.164 number instead, and WhatsApp's people.txt adds
  people who aren't contacts, which leaves Contacts as it was.
- ``timeline.json``: the documented events the conversations are built on, with sources.
- ``chats/*.txt``: one conversation per file, written like a transcript (messages.py).
- ``calls.txt`` (optional): call history (calls.py).
- ``voicemails.txt`` (optional): voicemail (voicemail.py).
- ``notes/*.txt`` (optional): one note per file (notes.py).
- ``safari.txt`` (optional): Safari history, one line per visit (safari.py).
- ``photos.json`` (optional): the camera roll and its People album (photos.py).
- ``whatsapp/*.txt`` (optional): WhatsApp chats and who's on WhatsApp (whatsapp.py).
- ``calendar.json`` (optional): the Calendar app's calendars and events (calendar.py).
- ``voice_memos.txt`` (optional): Voice Memos recordings and transcripts (voice_memos.py).

Each of those modules in this package (an "area", listed in core.AREAS) documents its files'
format and has ``load(pack)``, which parses them and returns the Persona attributes they fill and
their dated records (core.AreaData); contacts.py reads the cards (Contact), before the areas,
which name people by card id; core.py holds what they share (Persona, Snapshot, PersonaError)
and load_persona, which runs the loaders and checks what spans them. A new pack file goes in its
area's module (or a new one named in core.AREAS) plus a line per new attribute in Persona.

The generator adds made-up detail (the seconds of a message, ids, file names) from random
numbers, and each data type has its own (generate.area_random): adding a chat to a pack
changes only the contents of its Messages database and attachments and the messages part of
the answer key, never another data type's, so packs can grow one data type at a time. (What a
pack file says about another data type still shows there: a voicemail with no missed call in
calls.txt adds that call to call history, and, being one more draw in Calls' own random
numbers, re-draws the seconds and ids of the calls after it.) The backup's own files still
move: Manifest.db's inode numbers and, in an encrypted backup, the keys and so the raw bytes of
files after a new one (writers/context.py). Each new data type draws only from its own numbers too (see
generate.STEPS).

Real dates are written as they happened. ``shift_years`` moves them onto the
device-era calendar (Einstein's 1905 plays out in 2021), so that backup formats
and iOS versions line up with real years. Wall-clock times are kept in the
persona's time zone, so summer dates pick up daylight saving the way a phone
would record them.

The public names the single persona.py module had are importable from here, so
``from backupgen.persona import Persona, load_persona`` keeps working. Private helpers (``_parse_*``
and the like) are only in the module that defines them. New code can import from that module;
new names needn't be added here.
"""

from backupgen import PERSONAS_DIR
from backupgen.container import Host
from backupgen.persona.calendar import (
    CALENDAR_ACCOUNTS,
    EVENT_STATUSES,
    FREQUENCIES,
    RSVPS,
    WEEKDAYS,
    CalendarDef,
    EventDef,
    Repeat,
)
from backupgen.persona.calls import CALL_SERVICES, Call
from backupgen.persona.contacts import Contact
from backupgen.persona.core import AreaData, Pack, Persona, PersonaError, Snapshot, list_personas, load_persona
from backupgen.persona.messages import REACTIONS, Chat, Message
from backupgen.persona.notes import Note
from backupgen.persona.photos import Face, Photo, PhotoPerson
from backupgen.persona.safari import Visit
from backupgen.persona.voice_memos import VoiceMemoDef
from backupgen.persona.voicemail import Voicemail
from backupgen.persona.whatsapp import WA_MISSING_PHOTO, WAChat, WAMessage, WAPerson

__all__ = [
    "PERSONAS_DIR", "Host",
    "CALENDAR_ACCOUNTS", "EVENT_STATUSES", "FREQUENCIES", "RSVPS", "WEEKDAYS", "CalendarDef", "EventDef", "Repeat",
    "CALL_SERVICES", "Call",
    "AreaData", "Contact", "Pack", "Persona", "PersonaError", "Snapshot", "list_personas", "load_persona",
    "REACTIONS", "Chat", "Message",
    "Note",
    "Face", "Photo", "PhotoPerson",
    "Visit",
    "VoiceMemoDef",
    "Voicemail",
    "WA_MISSING_PHOTO", "WAChat", "WAMessage", "WAPerson",
]
