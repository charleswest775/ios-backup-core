"""WhatsApp: ChatStorage.sqlite and what goes with it, in the app's shared container.

Everything lives in the AppDomainGroup-group.net.whatsapp.WhatsApp.shared domain:

- ``ChatStorage.sqlite``, a Core Data store: chats (ZWACHATSESSION), messages (ZWAMESSAGE),
  media (ZWAMEDIAITEM), receipts and reactions (ZWAMESSAGEINFO), groups and their members
  (ZWAGROUPINFO, ZWAGROUPMEMBER) and the names people gave WhatsApp (ZWAPROFILEPUSHNAME).
- ``LID.sqlite`` (WhatsApp 2.23 on), which pairs LIDs (the ids newer groups address members
  by) with phone numbers, or ``ContactsV2.sqlite`` (up to WhatsApp 2.23), WhatsApp's copy of
  the address book. Which ones a backup has follows the harvested schema it is built from.
- ``Library/Preferences/group.net.whatsapp.WhatsApp.shared.plist`` with the owner's
  OwnJabberID, LID and WhatsApp name.
- Media under ``Message/Media/<chat JID>/<x>/<y>/<uuid>.<ext>``, with ``.thumb`` previews.

Rows follow what the public test backups showed (WhatsApp 2.20 to 2.24, see the
WhatsApp extractor in ios-backup-core for the details):

- ZSORT is the order messages arrived in each chat (1, 2, 3...); Z_PK is insertion order
  across all chats; ZMESSAGEDATE is seconds since 2001. ZMESSAGECOUNTER is the next ZSORT.
- System notices are type 10 (event 2: the encryption notice) and type 6 (event 12: group
  created; event 2: someone was added), addressed (ZTOJID) to the owner.
- A reply is a text message with a media row whose ZMETADATA (protobuf) names the quoted
  message: 5 its stanza id, 6 its sender (left out when it's the owner's), 19 a copy.
- Reactions are not rows. They sit in ZWAMESSAGEINFO.ZRECEIPTINFO of the message they react
  to: field 7.1 others' ({2 JID, 3 emoji, 4 ms since 1970}), 7.2 the owner's ({2 emoji, 3 ms}).
- Calls are type 59 rows from WhatsApp 2.24 on, ZMETADATA 87.1 = {1 video, 2 outcome, 3
  seconds}. Earlier versions keep calls in a CallHistory.sqlite of their own, which this
  writer doesn't make, so a pack's calls are left out of older layouts.
- Images keep their height and width in ZLATITUDE/ZLONGITUDE and their MIME type in
  ZVCARDSTRING; ZTITLE is the caption (or a document's file name, or a location's place).
- From WhatsApp 2.23, ZPUSHNAME and ZLASTMESSAGETEXT hold base64 protobuf, not text.

Simplifications: a broadcast message isn't copied into each recipient's own chat, as
WhatsApp does; documents have no preview thumbnail; voice notes are silence.
"""

from __future__ import annotations

import base64
import hashlib
import io
import plistlib
import random
import re
import struct
import textwrap
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

from backupgen import SCHEMAS_DIR
from backupgen.media import placeholder
from backupgen.persona import WA_MISSING_PHOTO, Persona, Snapshot, WAChat, WAMessage
from backupgen.sqlitegen import build, load_schema
from backupgen.writers import CoreData, cocoa_seconds

DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
PLIST = "Library/Preferences/group.net.whatsapp.WhatsApp.shared.plist"
STATUS_JID = "status@broadcast"
CALLS_IN_CHAT_FROM = (17, 3)  # the first harvested layout (WhatsApp 2.24) with type-59 call rows

TEXT, IMAGE, AUDIO, LOCATION, GROUP_EVENT, DOCUMENT, SYSTEM, DELETED, CALL = 0, 1, 3, 5, 6, 8, 10, 14, 59
_SESSION_TYPES = {"individual": 0, "group": 1, "broadcast": 2, "status": 2}
_KINDS = {"text": "text", "photo": "image", "voice": "audio", "document": "document", "location": "shared_location",
          "live_location": "live_location", "call": "call", "deleted": "deleted", "notice": "system"}
_CALL_OUTCOMES = {"answered": 0, "missed": 1, "unanswered": 0}
# What a reply or a reaction can point at: not notices, calls, deletions or other reactions.
_ANSWERABLE = ("text", "photo", "voice", "document", "location", "live_location")


@dataclass
class WrittenChat:
    """One chat as written, for the answer key."""

    id: str
    jid: str
    type: str
    name: str  # what a reader should call it
    handle: str  # "+digits" in a one-to-one chat
    contact: bool  # the other person is in the address book
    participants: list[str]  # "+digits"
    message_count: int = 0
    last_message_date: Optional[datetime] = None


@dataclass
class WrittenMessage:
    """One message as written, for the answer key."""

    chat_id: str
    sort: int
    pk: int
    date: datetime
    is_from_me: bool
    sender_handle: str  # "" for the owner and for notices
    sender_name: str
    kind: str  # the reader's message_type
    text: str
    starred: bool = False
    quoted: Optional[dict] = None
    reactions: list = field(default_factory=list)
    call: Optional[dict] = None
    location: Optional[dict] = None
    attachment: Optional[dict] = None
    notice: Optional[dict] = None


@dataclass
class Store:
    """What write() produced: the files to add to the backup and what they hold."""

    schema: str  # the harvested ChatStorage layout used
    owner: str
    files: dict = field(default_factory=dict)  # (domain, relative path) -> bytes
    chats: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    status_updates: int = 0
    calls_left_out: int = 0  # calls the pack has that this layout keeps outside the chats


@dataclass
class _Row:
    chat: WAChat
    msg: WAMessage
    when: datetime
    position: int  # in the chat file
    sort: int = 0
    pk: int = 0
    stanza: str = ""
    quoted: Optional["_Row"] = None
    reactions: list = field(default_factory=list)  # (who, emoji, when)
    written: Optional[WrittenMessage] = None


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def _has_schema(version: str, database: str) -> bool:
    return (SCHEMAS_DIR / f"ios-{version}" / f"{database}.json").is_file()


def digits(number: str) -> str:
    return re.sub(r"\D", "", number)


# ── protobuf, the way WhatsApp stores replies, reactions and calls ──────────

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def pb(*fields) -> bytes:
    """Encode ``(number, value)`` pairs: ints as varints, str/bytes length-delimited, lists as nested messages."""
    out = bytearray()
    for number, value in fields:
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int):
            out += _varint(number << 3) + _varint(value)
            continue
        if isinstance(value, str):
            value = value.encode("utf-8")
        elif isinstance(value, (list, tuple)):
            value = pb(*value)
        out += _varint(number << 3 | 2) + _varint(len(value)) + value
    return bytes(out)


def _unix_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


# ── media ────────────────────────────────────────────────────────────────────

_CRC_TABLE = []
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        _r = ((_r << 1) ^ 0x04C11DB7) if _r & 0x80000000 else (_r << 1)
        _r &= 0xFFFFFFFF
    _CRC_TABLE.append(_r)

SILENT_OPUS_FRAME = b"\xf8\xff\xfe"  # 20 ms of silence: one CELT frame, the one encoders send between words


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ b) & 0xFF]
    return crc


def _ogg_page(packets: list[bytes], granule: int, serial: int, sequence: int, flags: int) -> bytes:
    lacing = bytearray()
    for packet in packets:
        size = len(packet)
        while size >= 255:
            lacing.append(255)
            size -= 255
        lacing.append(size)
    page = bytearray(b"OggS" + bytes([0, flags]) + struct.pack("<qII", granule, serial, sequence)
                     + b"\0\0\0\0" + bytes([len(lacing)]) + lacing + b"".join(packets))
    struct.pack_into("<I", page, 22, _ogg_crc(bytes(page)))
    return bytes(page)


def ogg_opus(seconds: int, serial: int) -> bytes:
    """A real Ogg Opus voice note of ``seconds`` of silence (RFC 7845), mono, as WhatsApp records at 16 kHz."""
    pre_skip = 312
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, pre_skip, 16000, 0, 0)
    vendor = b"backupgen"
    tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    pages = [_ogg_page([head], 0, serial, 0, 0x02), _ogg_page([tags], 0, serial, 1, 0)]
    frames, done, sequence = max(1, seconds) * 50, 0, 2
    while done < frames:
        count = min(50, frames - done)
        done += count
        pages.append(_ogg_page([SILENT_OPUS_FRAME] * count, pre_skip + done * 960, serial, sequence,
                               0x04 if done == frames else 0))
        sequence += 1
    return b"".join(pages)


def _pdf_text(text: str) -> str:
    raw = text.encode("cp1252", "replace")
    out = []
    for b in raw:
        ch = chr(b)
        if ch in "()\\":
            out.append("\\" + ch)
        elif 32 <= b < 127:
            out.append(ch)
        else:
            out.append(f"\\{b:03o}")
    return "".join(out)


def pdf(text: str) -> bytes:
    """A one-page PDF: the first line as a heading, the rest as body text (Helvetica, US Letter)."""
    title, _, body = text.partition("\n")
    lines = []
    for paragraph in body.split("\n"):
        lines.extend(textwrap.wrap(paragraph, 88) or [""])
    ops = [f"BT /F1 18 Tf 72 720 Td ({_pdf_text(title)}) Tj ET", "BT /F1 11 Tf 16 TL 72 690 Td"]
    ops += [f"({_pdf_text(line)}) Tj T*" for line in lines[:38]]
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Title (" + _pdf_text(title).encode("latin-1") + b") >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body_bytes in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body_bytes + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _jpeg(image: Image.Image, quality: int) -> bytes:
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=quality)
    return out.getvalue()


def _thumb(image: Image.Image, edge: int = 200) -> bytes:
    small = image.convert("RGB")
    small.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    return _jpeg(small, 70)


def whatsapp_photo(camera_jpeg: bytes) -> tuple[bytes, bytes, int, int]:
    """(photo, thumbnail, width, height) as WhatsApp keeps a sent photo: recompressed, no camera details."""
    with Image.open(io.BytesIO(camera_jpeg)) as image:
        image = image.convert("RGB")
        if max(image.size) > 1600:
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        return _jpeg(image, 70), _thumb(image), image.width, image.height


def map_preview(rng: random.Random, width: int = 480, height: int = 240) -> bytes:
    """A drawn street-map tile with a pin in the middle, like WhatsApp's saved location preview."""
    image = Image.new("RGB", (width, height), "#f2efe9")
    draw = ImageDraw.Draw(image)
    for _ in range(2):  # parks
        x, y = rng.randrange(width), rng.randrange(height)
        draw.ellipse([x - rng.randrange(40, 120), y - rng.randrange(30, 80), x + rng.randrange(40, 120),
                      y + rng.randrange(30, 80)], fill="#c8e6c1")
    if rng.random() < 0.6:  # water
        top = rng.randrange(height)
        draw.polygon([(0, top), (width, top + rng.randrange(-60, 60)), (width, height), (0, height)], fill="#aad3df")
    for i in range(7):  # streets
        wide = i < 2
        if rng.random() < 0.5:
            y = rng.randrange(height)
            line = [(0, y), (width, y + rng.randrange(-40, 40))]
        else:
            x = rng.randrange(width)
            line = [(x, 0), (x + rng.randrange(-40, 40), height)]
        draw.line(line, fill="#d9d0c1", width=11 if wide else 6)
        draw.line(line, fill="#ffffff" if not wide else "#fbe6a2", width=8 if wide else 4)
    cx, cy = width // 2, height // 2
    draw.ellipse([cx - 12, cy - 36, cx + 12, cy - 12], fill="#e5483c", outline="#a8291f")
    draw.polygon([(cx - 10, cy - 20), (cx + 10, cy - 20), (cx, cy)], fill="#e5483c")
    draw.ellipse([cx - 4, cy - 28, cx + 4, cy - 20], fill="#ffffff")
    return _jpeg(image, 82)


# ── the store ────────────────────────────────────────────────────────────────

class _Writer:
    def __init__(self, persona: Persona, snapshot: Snapshot, rng: random.Random, renderer):
        self.persona, self.snapshot, self.rng, self.renderer = persona, snapshot, rng, renderer
        self.version = load_schema(snapshot.schema, "ChatStorage.sqlite")["ios"]
        self.has_lid = _has_schema(self.version, "LID.sqlite")
        self.has_contacts_v2 = _has_schema(self.version, "ContactsV2.sqlite")
        self.calls_in_chat = _version(self.version) >= CALLS_IN_CHAT_FROM
        self.people = persona.whatsapp_people
        owner_phone = persona.owner.phones[0][1] if persona.owner.phones else ""
        if not owner_phone:
            raise ValueError(f"{persona.id}: WhatsApp needs the owner's phone number")
        self.owner = digits(owner_phone)
        self.owner_jid = f"{self.owner}@s.whatsapp.net"
        self.store = Store(self.version, "+" + self.owner)
        self.lids: dict[str, str] = {}  # person id ("me" for the owner) -> LID digits
        self.media_pk = 0
        self.info_pk = 0

    # identities
    def lid(self, who: str) -> str:
        if who not in self.lids:
            while True:
                candidate = str(self.rng.randrange(10 ** 14, 10 ** 15))
                if candidate not in self.lids.values():
                    break
            self.lids[who] = candidate
        return self.lids[who]

    def person_jid(self, who: str, chat: Optional[WAChat] = None) -> str:
        if chat is not None and chat.lid and self.has_lid:
            return f"{self.lid(who)}@lid"
        return self.owner_jid if who == "me" else f"{digits(self.people[who].number)}@s.whatsapp.net"

    def handle(self, who: str) -> str:
        return "" if who in ("me", "notice") else self.people[who].number

    def name(self, who: str) -> str:
        if who in ("me", "notice"):
            return ""
        person = self.people[who]
        return person.contact.display_name if person.contact else person.push_name

    def stanza(self) -> str:
        return "3A" + self.rng.randbytes(9).hex().upper()

    def push_name_blob(self, when: datetime) -> str:
        # From WhatsApp 2.23 ZPUSHNAME holds base64 protobuf rather than a name.
        return base64.b64encode(pb((1, int(when.timestamp())), (4, 0))).decode()

    def media_path(self, chat_jid: str, ext: str) -> str:
        hexdigits = "0123456789abcdef"
        name = str(uuid.UUID(bytes=self.rng.randbytes(16)))
        return f"Media/{chat_jid}/{self.rng.choice(hexdigits)}/{self.rng.choice(hexdigits)}/{name}.{ext}"

    def add_media_file(self, rel: str, data: bytes) -> None:
        self.store.files[(DOMAIN, "Message/" + rel)] = data

    # building
    def timeline(self) -> list[tuple[WAChat, list[_Row]]]:
        chats = []
        for chat in self.persona.whatsapp or []:
            rows, previous = [], None
            for position, message in enumerate(chat.messages):
                when = self.persona.device_time(message.real_time) + timedelta(seconds=self.rng.randrange(60))
                if previous is not None and when <= previous:
                    when = previous + timedelta(seconds=1 + self.rng.randrange(5))
                previous = when
                if when > self.snapshot.backup_date:
                    continue
                if message.kind == "call" and not self.calls_in_chat:
                    self.store.calls_left_out += 1
                    continue
                rows.append(_Row(chat, message, when, position))
            # Replies and reactions point at the latest answerable message from whoever they name.
            kept: list[_Row] = []
            for row in rows:
                m = row.msg
                if m.kind == "reaction":
                    target = next((t for t in reversed(kept) if t.msg.sender != m.sender
                                   and t.msg.kind in _ANSWERABLE), None)
                    if target is None:
                        raise ValueError(f"{m.where}: a reaction with nothing to react to")
                    target.reactions.append((m.sender, m.reaction, row.when))
                    continue
                if m.reply_to:
                    row.quoted = next((t for t in reversed(kept) if t.msg.sender == m.reply_to
                                       and t.msg.kind in _ANSWERABLE), None)
                    if row.quoted is None:
                        raise ValueError(f"{m.where}: nothing from {m.reply_to} to reply to")
                kept.append(row)
            for sort, row in enumerate(kept, 1):
                row.sort = sort
                row.stanza = self.stanza()
            if kept:
                chats.append((chat, kept))
        return chats

    def chat_jid(self, chat: WAChat, rows: list[_Row]) -> str:
        if chat.type == "individual":
            return self.person_jid(chat.participants[0])
        if chat.type == "group":
            return f"120363{self.rng.randrange(10 ** 11, 10 ** 12)}@g.us"
        if chat.type == "broadcast":
            return f"{int(rows[0].when.timestamp())}@broadcast"
        return STATUS_JID

    def populate(self, db) -> None:
        core = CoreData(db)
        sessions, messages, media = db.table("ZWACHATSESSION"), db.table("ZWAMESSAGE"), db.table("ZWAMEDIAITEM")
        infos, groups, members = db.table("ZWAMESSAGEINFO"), db.table("ZWAGROUPINFO"), db.table("ZWAGROUPMEMBER")
        push_names = db.table("ZWAPROFILEPUSHNAME")

        chats = self.timeline()
        chats.sort(key=lambda entry: entry[1][0].when)  # sessions are created as chats start
        jids = {chat.id: self.chat_jid(chat, rows) for chat, rows in chats}
        session_pks = {chat.id: n for n, (chat, _) in enumerate(chats, 1)}
        member_pks: dict[tuple[str, str], int] = {}
        group_pks: dict[str, int] = {}
        member_pk = 0
        for chat, rows in chats:
            if chat.type != "group":
                continue
            created = int(cocoa_seconds(rows[0].when))
            group_pk = len(group_pks) + 1
            group_pks[chat.id] = group_pk
            creator = self.person_jid(chat.creator, chat)
            groups.insert({
                "Z_PK": group_pk, "Z_ENT": core.ent("WAGroupInfo"), "Z_OPT": 1, "ZSTATE": 0,
                "ZCHATSESSION": session_pks[chat.id], "ZCREATIONDATE": created, "ZSUBJECTTIMESTAMP": created,
                "ZCREATORJID": creator, "ZSUBJECTOWNERJID": creator,
            })
            core.saw("WAGroupInfo", group_pk)
            for who in ["me", *chat.participants]:
                member_pk += 1
                person = self.people.get(who)
                members.insert({
                    "Z_PK": member_pk, "Z_ENT": core.ent("WAGroupMember"), "Z_OPT": 1, "ZCONTACTABID": 0,
                    "ZISACTIVE": 1, "ZISADMIN": int(who == chat.creator), "ZCHATSESSION": session_pks[chat.id],
                    "ZCONTACTNAME": person.contact.display_name if person and person.contact else "",
                    "ZMEMBERJID": self.person_jid(who, chat),
                })
                member_pks[(chat.id, who)] = member_pk
                core.saw("WAGroupMember", member_pk)

        # Messages get keys in the order they were stored, across all chats.
        everything = sorted((row for _, rows in chats for row in rows),
                            key=lambda r: (r.when, session_pks[r.chat.id], r.position))
        for pk, row in enumerate(everything, 1):
            row.pk = pk
        for row in everything:
            self.message(core, row, session_pks[row.chat.id], jids[row.chat.id], member_pks, messages, media, infos)

        for chat, rows in chats:
            last = rows[-1]
            self.session(core, sessions, chat, rows, session_pks[chat.id], jids[chat.id], group_pks.get(chat.id))
            if chat.type == "status":
                self.store.status_updates += len(rows)
                continue
            self.store.chats.append(WrittenChat(
                id=chat.id, jid=jids[chat.id], type=chat.type, name=self.chat_name(chat),
                handle=self.handle(chat.participants[0]) if chat.type == "individual" else "",
                contact=chat.type == "individual" and self.people[chat.participants[0]].contact is not None,
                participants=[self.handle(p) for p in chat.participants],
                message_count=len(rows), last_message_date=last.when,
            ))
            self.store.messages.extend(row.written for row in rows)

        seen = sorted({p for chat, _ in chats for p in chat.participants})
        for n, who in enumerate(seen, 1):
            push_names.insert({"Z_PK": n, "Z_ENT": core.ent("WAProfilePushName"), "Z_OPT": 1,
                               "ZJID": self.person_jid(who), "ZPUSHNAME": self.people[who].push_name})
            core.saw("WAProfilePushName", n)
        core.saw("WAMessage", len(everything))
        core.saw("WAChatSession", len(chats))
        core.saw("WAMediaItem", self.media_pk)
        core.saw("WAMessageInfo", self.info_pk)
        core.finish()

    def chat_name(self, chat: WAChat) -> str:
        return chat.name if chat.type in ("group", "broadcast") else self.name(chat.participants[0])

    def session(self, core, sessions, chat, rows, pk, jid, group_pk) -> None:
        last = rows[-1]
        if chat.type == "individual":
            person = self.people[chat.participants[0]]
            partner = person.contact.display_name if person.contact else _formatted(person.number)
        else:
            partner = chat.name
        preview = last.msg.text or {"photo": "Photo", "voice": "Voice message", "document": last.msg.document,
                                    "location": "Location", "live_location": "Live location"}.get(last.msg.kind, "")
        sessions.insert({
            "Z_PK": pk, "Z_ENT": core.ent("WAChatSession"), "Z_OPT": 1, "ZARCHIVED": 0, "ZCONTACTABID": 0,
            "ZFLAGS": 0, "ZHIDDEN": 0, "ZIDENTITYVERIFICATIONEPOCH": 0, "ZIDENTITYVERIFICATIONSTATE": 0,
            "ZMESSAGECOUNTER": last.sort + 1, "ZREMOVED": 0, "ZSESSIONTYPE": _SESSION_TYPES[chat.type],
            "ZSPOTLIGHTSTATUS": 0, "ZUNREADCOUNT": 0, "ZGROUPINFO": group_pk, "ZLASTMESSAGE": last.pk,
            "ZLASTMESSAGEDATE": cocoa_seconds(last.when), "ZCONTACTJID": jid,
            "ZLASTMESSAGETEXT": self.push_name_blob(last.when) if self.has_lid else preview,
            "ZPARTNERNAME": partner if chat.type != "status" else "",
        })

    def media_row(self, core, media, msg_pk: int, **columns) -> int:
        self.media_pk += 1
        media.insert({"Z_PK": self.media_pk, "Z_ENT": core.ent("WAMediaItem"), "Z_OPT": 1, "ZMESSAGE": msg_pk,
                      "ZFILESIZE": 0, "ZMEDIAORIGIN": 0, "ZMOVIEDURATION": 0, "ZASPECTRATIO": 0.0,
                      "ZHACCURACY": 0.0, "ZLATITUDE": 0.0, "ZLONGITUDE": 0.0, **columns})
        return self.media_pk

    def quote_meta(self, row: _Row, chat_jid: str) -> bytes:
        target = row.quoted
        fields: list = [(5, target.stanza)]
        if target.msg.sender != "me":
            fields.append((6, self.person_jid(target.msg.sender, row.chat)))
        m = target.msg
        if m.kind == "text":
            content = [(1, m.text)]
        elif m.kind == "photo":
            content = [(3, [(2, "image/jpeg")] + ([(3, m.text)] if m.text else []))]
        elif m.kind == "voice":
            content = [(8, [(2, "audio/ogg; codecs=opus"), (6, True)])]
        elif m.kind == "document":
            content = [(7, [(2, "application/pdf"), (8, m.document)])]
        else:  # a location
            content = [(5, [(5, m.place)] if m.place else [])]
        fields.append((19, content))
        return pb(*fields)

    def message(self, core, row: _Row, session_pk: int, chat_jid: str, member_pks, messages, media, infos) -> None:
        m, chat = row.msg, row.chat
        from_me = m.sender == "me"
        notice = m.kind == "notice"
        date = cocoa_seconds(row.when)
        values = {
            "Z_PK": row.pk, "Z_ENT": core.ent("WAMessage"), "Z_OPT": 2, "ZCHILDMESSAGESDELIVEREDCOUNT": 0,
            "ZCHILDMESSAGESPLAYEDCOUNT": 0, "ZCHILDMESSAGESREADCOUNT": 0, "ZDATAITEMVERSION": 0, "ZDOCID": 0,
            "ZENCRETRYCOUNT": 0, "ZFILTEREDRECIPIENTCOUNT": 0, "ZFLAGS": 0, "ZISFROMME": int(from_me),
            "ZGROUPEVENTTYPE": 0 if from_me else 2, "ZMESSAGEERRORSTATUS": 0, "ZMESSAGESTATUS": 8 if from_me else 0,
            "ZSORT": row.sort, "ZSPOTLIGHTSTATUS": 0, "ZSTARRED": 1 if m.starred else None,
            "ZCHATSESSION": session_pk, "ZMESSAGEDATE": date, "ZSTANZAID": row.stanza,
            "ZPUSHNAME": self.push_name_blob(row.when) if self.has_lid else (
                None if from_me or notice else self.people[m.sender].push_name),
        }
        if notice:
            values.update(ZISFROMME=0, ZFROMJID=chat_jid, ZTOJID=self.owner_jid, ZMESSAGESTATUS=6, ZPUSHNAME=None)
        elif from_me:
            values.update(ZTOJID=chat_jid, ZSENTDATE=date + 1 + self.rng.randrange(3))
        else:
            values.update(ZFROMJID=chat_jid if chat.type != "status" else self.person_jid(m.sender),
                          ZSENTDATE=date + 1 + self.rng.randrange(3))
            if chat.type == "group":
                values["ZGROUPMEMBER"] = member_pks[(chat.id, m.sender)]
        if chat.type == "broadcast":
            count = len(chat.participants)
            values.update(ZFILTEREDRECIPIENTCOUNT=count, ZCHILDMESSAGESDELIVEREDCOUNT=count,
                          ZCHILDMESSAGESREADCOUNT=max(0, count - 1 - self.rng.randrange(2)))

        written = WrittenMessage(chat.id, row.sort, row.pk, row.when, from_me and not notice, self.handle(m.sender),
                                 self.name(m.sender), _KINDS[m.kind], m.text if m.kind in ("text", "photo") else "",
                                 starred=m.starred)
        media_columns: Optional[dict] = None
        files: list[tuple[str, bytes]] = []

        if notice:
            if m.notice == "encrypted":
                values.update(ZMESSAGETYPE=SYSTEM, ZGROUPEVENTTYPE=2)
                written.notice = {"event": "encrypted"}
            elif m.notice == "created":
                values.update(ZMESSAGETYPE=GROUP_EVENT, ZGROUPEVENTTYPE=12)
                written.notice = {"event": "created", "by": "me" if chat.creator == "me" else self.handle(chat.creator),
                                  "name": "You" if chat.creator == "me" else self.name(chat.creator)}
            else:
                values.update(ZMESSAGETYPE=GROUP_EVENT, ZGROUPEVENTTYPE=2)
                if m.member != "me":
                    values["ZGROUPMEMBER"] = member_pks[(chat.id, m.member)]
                written.notice = {"event": "added", "member": "me" if m.member == "me" else self.handle(m.member),
                                  "name": "You" if m.member == "me" else self.name(m.member)}
        elif m.kind == "text":
            values.update(ZMESSAGETYPE=TEXT, ZTEXT=m.text)
            if row.quoted is not None:
                media_columns = {"ZMETADATA": self.quote_meta(row, chat_jid)}
                target = row.quoted.written
                written.quoted = {"sort": row.quoted.sort, "sender_handle": target.sender_handle,
                                  "is_from_me": target.is_from_me, "text": target.text, "kind": target.kind}
        elif m.kind == "photo":
            values["ZMESSAGETYPE"] = IMAGE
            path = self.media_path(chat_jid, "jpg")
            thumb_path = path[:-3] + "thumb"
            if m.photo == WA_MISSING_PHOTO:
                width, height = 1200, 1600
                frame = placeholder(width, height, "#7f9f86", random.Random(f"{self.persona.id}/{row.stanza}"))
                thumb, size, data = _thumb(frame), 180_000 + self.rng.randrange(120_000), None
            else:
                data, thumb, width, height = whatsapp_photo(self.renderer.jpeg(m.photo)[0])
                size = len(data)
                files.append((path, data))
            files.append((thumb_path, thumb))
            media_columns = {"ZMEDIALOCALPATH": path, "ZXMPPTHUMBPATH": thumb_path, "ZFILESIZE": size,
                             "ZLATITUDE": float(height), "ZLONGITUDE": float(width), "ZVCARDSTRING": "image/jpeg",
                             "ZVCARDNAME": base64.b64encode(hashlib.sha256(data or thumb).digest()).decode(),
                             "ZTITLE": m.text or None, "ZMEDIAKEY": self.rng.randbytes(32),
                             "ZASPECTRATIO": round(width / height, 4)}
            written.attachment = {"name": path.rsplit("/", 1)[1], "path": "Message/" + path, "mime_type": "image/jpeg",
                                  "bytes": size, "available": data is not None, "thumbnail": "Message/" + thumb_path,
                                  "width": width, "height": height, "duration": None}
        elif m.kind == "voice":
            values["ZMESSAGETYPE"] = AUDIO
            path = self.media_path(chat_jid, "opus")
            data = ogg_opus(m.duration, self.rng.getrandbits(32))
            files.append((path, data))
            media_columns = {"ZMEDIALOCALPATH": path, "ZFILESIZE": len(data), "ZMOVIEDURATION": m.duration,
                             "ZVCARDSTRING": "audio/ogg; codecs=opus", "ZMEDIAKEY": self.rng.randbytes(32)}
            written.attachment = {"name": path.rsplit("/", 1)[1], "path": "Message/" + path,
                                  "mime_type": "audio/ogg; codecs=opus", "bytes": len(data), "available": True,
                                  "thumbnail": None, "width": None, "height": None, "duration": m.duration}
        elif m.kind == "document":
            values["ZMESSAGETYPE"] = DOCUMENT
            path = self.media_path(chat_jid, "pdf")
            data = pdf(self.persona.whatsapp_documents[m.document])
            files.append((path, data))
            media_columns = {"ZMEDIALOCALPATH": path, "ZFILESIZE": len(data), "ZMOVIEDURATION": 1,
                             "ZVCARDSTRING": "application/pdf", "ZTITLE": m.document,
                             "ZMEDIAKEY": self.rng.randbytes(32)}
            written.attachment = {"name": m.document, "path": "Message/" + path, "mime_type": "application/pdf",
                                  "bytes": len(data), "available": True, "thumbnail": None, "width": None,
                                  "height": None, "duration": None}
        elif m.kind in ("location", "live_location"):
            values["ZMESSAGETYPE"] = LOCATION
            thumb_path = self.media_path(chat_jid, "thumb")
            files.append((thumb_path, map_preview(random.Random(f"{self.persona.id}/{row.stanza}"))))
            live = m.kind == "live_location"
            media_columns = {"ZLATITUDE": m.lat, "ZLONGITUDE": m.lon, "ZMEDIAORIGIN": int(live),
                             "ZXMPPTHUMBPATH": thumb_path, "ZTITLE": m.place or None, "ZHACCURACY": 5.0 if live else 0.0}
            written.location = {"lat": m.lat, "lon": m.lon, "live": live, "place": m.place or None,
                                "thumbnail": "Message/" + thumb_path}
        elif m.kind == "call":
            values["ZMESSAGETYPE"] = CALL
            seconds = m.duration if m.outcome == "answered" else 0
            partner = self.person_jid(chat.participants[0])
            media_columns = {"ZMETADATA": pb((87, [(1, [(1, m.video), (2, _CALL_OUTCOMES[m.outcome]), (3, seconds),
                                                        (4, 0), (5, [(1, partner), (2, 0)])]),
                                                  (2, 0), (3, 1), (4, 0)]))}
            written.call = {"video": m.video, "outcome": m.outcome, "duration": seconds,
                            "direction": "outgoing" if from_me else "incoming"}
        elif m.kind == "deleted":
            values["ZMESSAGETYPE"] = DELETED

        if media_columns is not None:
            values["ZMEDIAITEM"] = self.media_row(core, media, row.pk, **media_columns)
        if row.reactions:
            self.info_pk += 1
            block, entries = [], []
            for who, emoji, when in sorted(row.reactions, key=lambda r: r[2]):
                if who == "me":
                    block.append((2, [(1, self.stanza()), (2, emoji), (3, _unix_ms(when))]))
                else:
                    block.append((1, [(1, self.rng.randbytes(16).hex().upper()), (2, self.person_jid(who, chat)),
                                      (3, emoji), (4, _unix_ms(when))]))
                entries.append({"emoji": emoji, "sender_handle": self.handle(who), "is_from_me": who == "me",
                                "date": when.replace(microsecond=0).isoformat()})
            infos.insert({"Z_PK": self.info_pk, "Z_ENT": core.ent("WAMessageInfo"), "Z_OPT": 1, "ZMESSAGE": row.pk,
                          "ZRECEIPTINFO": pb((4, 2), (7, block))})
            values["ZMESSAGEINFO"] = self.info_pk
            written.reactions = entries
        for rel, data in files:
            self.add_media_file(rel, data)
        row.written = written
        messages.insert(values)

    # side files
    def populate_lid(self, db) -> None:
        core = CoreData(db)
        pairs = db.table("ZWAPHONENUMBERLIDPAIR")
        first = min((m.real_time for chat in self.persona.whatsapp or [] for m in chat.messages), default=None)
        stamp = cocoa_seconds(self.persona.device_time(first)) if first else 0.0
        self.lid("me")
        for n, (who, lid) in enumerate(sorted(self.lids.items(), key=lambda kv: kv[1]), 1):
            phone = self.owner if who == "me" else digits(self.people[who].number)
            pairs.insert({"Z_PK": n, "Z_ENT": core.ent("WAPhoneNumberLIDPair"), "Z_OPT": 1, "ZTIMESTAMP": stamp,
                          "ZLID": lid, "ZPHONENUMBER": phone})
            core.saw("WAPhoneNumberLIDPair", n)
        core.finish()

    def populate_contacts_v2(self, db, people: list[str]) -> None:
        core = CoreData(db)
        table = db.table("ZWAADDRESSBOOKCONTACT")
        n = 0
        for who in people:
            person = self.people[who]
            if person.contact is None:
                continue
            n += 1
            number = digits(person.number)
            label, shown = person.contact.phones[0]
            table.insert({
                "Z_PK": n, "Z_ENT": core.ent("WAAddressBookContact"), "Z_OPT": 1, "ZPHONESTATUS": 1,
                "ZSECTIONSORT": 0, "ZSORT": n, "ZSPOTLIGHTSTATUS": 0, "ZFULLNAME": person.contact.display_name,
                "ZGIVENNAME": person.contact.first or None, "ZIDENTIFIER": str(uuid.UUID(bytes=self.rng.randbytes(16))).upper(),
                "ZLOCALIZEDPHONENUMBER": shown, "ZPHONENUMBER": shown, "ZPHONENUMBERLABEL": label,
                "ZSECTIONTITLE": (person.contact.display_name[:1] or "#").upper(),
                "ZWHATSAPPID": f"{number}@s.whatsapp.net" if self.version.startswith("15") else number,
            })
            core.saw("WAAddressBookContact", n)
        core.finish()


def _formatted(number: str) -> str:
    """How WhatsApp shows a number that isn't in the address book: +1 760-555-0150."""
    d = digits(number)
    if d.startswith("1") and len(d) == 11:
        return f"+1 {d[1:4]}-{d[4:7]}-{d[7:]}"
    return "+" + d


def write(tmp: Path, persona: Persona, snapshot: Snapshot, rng: random.Random, renderer) -> Store:
    """Build WhatsApp's files for this snapshot. Returns them with what they hold."""
    writer = _Writer(persona, snapshot, rng, renderer)
    store = writer.store
    store.files[(DOMAIN, "ChatStorage.sqlite")] = build(tmp / "ChatStorage.sqlite", writer.version,
                                                        "ChatStorage.sqlite", writer.populate)
    people = sorted({p for chat in persona.whatsapp or [] for p in chat.participants})
    if writer.has_lid:
        store.files[(DOMAIN, "LID.sqlite")] = build(tmp / "LID.sqlite", writer.version, "LID.sqlite",
                                                    writer.populate_lid)
    if writer.has_contacts_v2:
        store.files[(DOMAIN, "ContactsV2.sqlite")] = build(tmp / "ContactsV2.sqlite", writer.version,
                                                           "ContactsV2.sqlite",
                                                           lambda db: writer.populate_contacts_v2(db, people))
    prefs = {"OwnJabberID": writer.owner_jid, "FullUserName": persona.owner.first or persona.owner.display_name}
    if writer.has_lid:
        prefs["lid"] = f"{writer.lid('me')}@lid"
    store.files[(DOMAIN, PLIST)] = plistlib.dumps(prefs, fmt=plistlib.FMT_BINARY)
    store.messages.sort(key=lambda w: (w.chat_id, w.sort))
    return store


def answer_key(store: Optional[Store]) -> Optional[dict]:
    if store is None:
        return None

    def iso(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat()

    return {
        "owner": store.owner,
        "schema": store.schema,
        "chats": [
            {"chat": c.id, "jid": c.jid, "type": c.type, "name": c.name, "handle": c.handle, "contact": c.contact,
             "participants": c.participants, "message_count": c.message_count,
             "last_message_date": iso(c.last_message_date)}
            for c in sorted(store.chats, key=lambda c: c.id)
        ],
        "messages": [
            {"chat": w.chat_id, "sort": w.sort, "date": iso(w.date), "is_from_me": w.is_from_me,
             "sender_handle": w.sender_handle, "sender_name": w.sender_name, "kind": w.kind, "text": w.text,
             "starred": w.starred, "quoted": w.quoted, "reactions": w.reactions, "call": w.call,
             "location": w.location, "attachment": w.attachment, "notice": w.notice}
            for w in store.messages
        ],
        "status_updates": store.status_updates,
        "calls_left_out": store.calls_left_out,
    }
