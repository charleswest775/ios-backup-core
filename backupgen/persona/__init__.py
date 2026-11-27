"""Persona packs: who the phone belongs to, their contacts, their conversations, their timeline.

A pack is a folder under ``personas/``:

- ``persona.json``: owner, contacts, time shift, devices and backup snapshots. The pack and
  each snapshot may also have ``options``, a free-form JSON object (``{}`` when left out) of
  settings for the writers, read as ``Persona.options`` and ``Snapshot.options``. Nothing
  reads them yet; a writer that does documents its keys.
- ``timeline.json``: the documented events the conversations are built on, with sources.
- ``chats/*.txt``: one conversation per file, written like a transcript (messages.py).
- ``calls.txt`` and ``voicemails.txt`` (optional): call history and voicemail (calls.py).
- ``notes/*.txt`` (optional): one note per file (notes.py).
- ``safari.txt`` (optional): Safari history, one line per visit (safari.py).
- ``photos.json`` (optional): the camera roll and its People album (photos.py).
- ``whatsapp/*.txt`` (optional): WhatsApp chats and who's on WhatsApp (whatsapp.py).
- ``calendar.json`` (optional): the Calendar app's calendars and events (calendar.py).
- ``voice_memos.txt`` (optional): Voice Memos recordings and transcripts (voice_memos.py).

Each of those modules in this package documents its files' format and parses them; core.py
holds what they share (Persona, Snapshot, Contact, PersonaError) and load_persona.

Real dates are written as they happened. ``shift_years`` moves them onto the
device-era calendar (Einstein's 1905 plays out in 2021), so that backup formats
and iOS versions line up with real years. Wall-clock times are kept in the
persona's time zone, so summer dates pick up daylight saving the way a phone
would record them.

Every name the single persona.py module had is importable from here, so
``from backupgen.persona import Persona, load_persona`` keeps working. New code can import
from the module that defines a name; new names needn't be added here.
"""

from backupgen.persona.calendar import (  # noqa: F401
    _BY_DAY, _COLOR, _EVENT_KEYS, _REPEAT_KEYS, CALENDAR_ACCOUNTS, EVENT_STATUSES, FREQUENCIES, RSVPS, WEEKDAYS,
    CalendarDef, EventDef, Repeat, _check_person, _number, _occurrence, _parse_calendar, _parse_event, _parse_repeat,
    _when, _zone,
)
from backupgen.persona.calls import _CALL, _VOICEMAIL, CALL_SERVICES, Call, Voicemail, _parse_calls, _parse_voicemails  # noqa: F401
from backupgen.persona.core import (  # noqa: F401
    _DURATION, PERSONAS_DIR, Contact, Host, Persona, PersonaError, Snapshot, _check_who, _contact, _dated_lines,
    _default_handle, _seconds, list_personas, load_persona,
)
from backupgen.persona.messages import _LINE, REACTIONS, Chat, Message, _parse_chat  # noqa: F401
from backupgen.persona.notes import _NOTE_HEADERS, Note, _parse_note  # noqa: F401
from backupgen.persona.photos import _PETS, Face, Photo, PhotoPerson, _parse_face, _parse_photo_people, _parse_photos  # noqa: F401
from backupgen.persona.safari import _VISIT, Visit, _parse_visits  # noqa: F401
from backupgen.persona.voice_memos import _MEMO, _MEMO_FLAG, VoiceMemoDef, _parse_voice_memos  # noqa: F401
from backupgen.persona.whatsapp import (  # noqa: F401
    _WA_ANSWERABLE, _WA_HEADERS, _WA_LINE, _WA_PERSON, _WA_TYPES, WA_MISSING_PHOTO, WAChat, WAMessage, WAPerson,
    _parse_wa_chat, _parse_wa_people, _parse_whatsapp, _wa_tag,
)
