"""Messages: sms.db.

Rows follow what iOS writes:
- Messages get ROWIDs in time order across all chats.
- Dates are nanoseconds since 2001-01-01.
- Every body is in ``text`` and in a typedstream ``attributedBody``.
- Tapbacks are separate messages with ``associated_message_type`` 2000–2005,
  ``associated_message_guid = "p:0/<target guid>"`` and Apple's "Loved “…”" text.
- In a 1:1 chat, both directions use the other person's handle. In a group,
  the sender's handle is used, or 0 for the owner.
- A photo is a message whose text is U+FFFC (the attachment's place in the text), with
  an ``attachment`` row pointing at ~/Library/SMS/Attachments/xx/nn/<GUID>/<name>, which
  lives in the backup's MediaDomain. A tapback on it reads "Loved an image".
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from backupgen.persona import REACTIONS, Chat, Message, Persona, Snapshot
from backupgen.sqlitegen import SchemaDatabase
from backupgen.typedstream import attributed_body
from backupgen.writers import cocoa_nanoseconds, country_code

_REACTION_VERBS = {
    "love": "Loved", "like": "Liked", "dislike": "Disliked",
    "laugh": "Laughed at", "emphasize": "Emphasized", "question": "Questioned",
}


@dataclass
class Written:
    """One message as written, for the answer key."""

    guid: str
    chat_id: str
    chat_identifier: str
    date: datetime
    is_from_me: bool
    sender_handle: str
    text: str
    is_reaction: bool
    attachment: Optional[tuple[str, str, bytes]] = None  # (MediaDomain path, file name, bytes)


def _guid(rng: random.Random) -> str:
    return str(uuid.UUID(bytes=rng.randbytes(16))).upper()


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def chat_identifier(persona: Persona, chat: Chat, room: str) -> str:
    return room if chat.is_group else persona.contacts[chat.participants[0]].handle


def populate(db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random,
             renderer=None) -> list[Written]:
    handles_t, chats_t, messages_t = db.table("handle"), db.table("chat"), db.table("message")
    chj, cmj = db.table("chat_handle_join"), db.table("chat_message_join")
    owner_email = persona.owner.emails[0][1] if persona.owner.emails else ""
    owner_phone = persona.owner.handle
    account_guid = _guid(rng)

    handle_ids: dict[tuple[str, str], int] = {}

    def handle_rowid(contact_id: str, service: str) -> int:
        handle = persona.contacts[contact_id].handle
        key = (handle, service)
        if key not in handle_ids:
            handle_ids[key] = handles_t.insert({
                "id": handle,
                "country": country_code(handle),
                "service": service,
                "uncanonicalized_id": handle if handle.startswith("+") else None,
                "person_centric_id": None,
            })
        return handle_ids[key]

    # Keep only what had happened before the backup was made. Messages written in the same minute
    # keep their order in the transcript, so a tapback never lands before what it reacts to.
    timeline: list[tuple[datetime, Chat, Message]] = []
    for chat in persona.chats:
        previous = None
        for message in chat.messages:
            when = persona.device_time(message.real_time) + timedelta(seconds=rng.randrange(60))
            if previous is not None and when <= previous:
                when = previous + timedelta(seconds=1 + rng.randrange(5))
            previous = when
            if when <= snapshot.backup_date:
                timeline.append((when, chat, message))
    timeline.sort(key=lambda entry: entry[0])
    active = {chat.id for _, chat, _ in timeline}

    chat_rows: dict[str, tuple[int, str, str]] = {}  # chat id → (ROWID, identifier, room name)
    for chat in persona.chats:
        if chat.id not in active:
            continue
        room = f"chat{rng.randrange(10**17, 10**18)}" if chat.is_group else ""
        identifier = chat_identifier(persona, chat, room)
        rowid = chats_t.insert({
            "guid": f"{chat.service};{'+' if chat.is_group else '-'};{identifier}",
            "style": 43 if chat.is_group else 45,
            "state": 3,
            "account_id": account_guid,
            "chat_identifier": identifier,
            "service_name": chat.service,
            "room_name": room or None,
            "account_login": f"E:{owner_email}" if chat.service == "iMessage" and owner_email else f"P:{owner_phone}",
            "is_archived": 0,
            "last_addressed_handle": owner_phone,
            "display_name": chat.name if chat.is_group else "",
            "group_id": _guid(rng),
            "is_filtered": 0,
            "successful_query": 1,
            "ck_sync_state": 0,
            "original_group_id": None,
        })
        for participant in chat.participants:
            chj.insert({"chat_id": rowid, "handle_id": handle_rowid(participant, chat.service)})
        chat_rows[chat.id] = (rowid, identifier, room)

    written: list[Written] = []
    latest: dict[str, list[tuple[str, str, str]]] = {}  # chat → [(sender, guid, text)]
    for when, chat, message in timeline:
        chat_rowid, identifier, room = chat_rows[chat.id]
        from_me = message.sender == "me"
        if chat.is_group:
            handle_id = 0 if from_me else handle_rowid(message.sender, chat.service)
        else:
            handle_id = handle_rowid(chat.participants[0], chat.service)
        guid = _guid(rng)
        associated_guid, associated_type, range_length = None, 0, 0
        if message.reaction:
            target = next((t for t in reversed(latest.get(chat.id, [])) if t[0] != message.sender), None)
            if target is None:
                raise ValueError(f"{message.where}: tapback with nothing to react to")
            _, target_guid, target_text = target
            if target_text is None:  # a photo
                text, range_length = f"{_REACTION_VERBS[message.reaction]} an image", 1
            else:
                text, range_length = f"{_REACTION_VERBS[message.reaction]} “{target_text}”", _utf16_length(target_text)
            associated_guid, associated_type = f"p:0/{target_guid}", REACTIONS[message.reaction]
        elif message.photo:
            text = "\ufffc"
        else:
            text = message.text
        date_ns = cocoa_nanoseconds(when)
        read_ns = cocoa_nanoseconds(when + timedelta(seconds=30 + rng.randrange(600))) if not from_me else 0
        delivered_ns = cocoa_nanoseconds(when + timedelta(seconds=1 + rng.randrange(4))) if from_me else 0
        rowid = messages_t.insert({
            "guid": guid,
            "text": text,
            "replace": 0,
            "handle_id": handle_id,
            "attributedBody": attributed_body(text),
            "version": 10,
            "type": 0,
            "service": chat.service,
            "account": f"E:{owner_email}" if chat.service == "iMessage" and owner_email else f"P:{owner_phone}",
            "account_guid": account_guid,
            "error": 0,
            "date": date_ns,
            "date_read": read_ns,
            "date_delivered": delivered_ns,
            "is_delivered": 1,
            "is_finished": 1,
            "is_emote": 0,
            "is_from_me": int(from_me),
            "is_empty": 0,
            "is_delayed": 0,
            "is_auto_reply": 0,
            "is_prepared": 0,
            "is_read": 0 if from_me else 1,
            "is_system_message": 0,
            "is_sent": int(from_me),
            "has_dd_results": 0,
            "is_service_message": 0,
            "is_forward": 0,
            "was_downgraded": 0,
            "is_archive": 0,
            "cache_has_attachments": int(bool(message.photo)),
            "cache_roomnames": room or None,
            "was_data_detected": 1,
            "was_deduplicated": 0,
            "is_audio_message": 0,
            "is_played": 0,
            "date_played": 0,
            "item_type": 0,
            "other_handle": 0,
            "group_action_type": 0,
            "share_status": 0,
            "share_direction": 0,
            "is_expirable": 0,
            "expire_state": 0,
            "message_action_type": 0,
            "message_source": 0,
            "associated_message_guid": associated_guid,
            "associated_message_type": associated_type,
            "associated_message_range_location": 0,
            "associated_message_range_length": range_length,
            "time_expressive_send_played": 0,
            "ck_sync_state": 0,
            "destination_caller_id": owner_phone if chat.service == "SMS" else (f"e:{owner_email}" if owner_email else owner_phone),
            "is_corrupt": 0,
            "sort_id": 0,
            "is_spam": 0,
            "has_unseen_mention": 0,
            "was_delivered_quietly": 0,
            "did_notify_recipient": 0,
        })
        cmj.insert({"chat_id": chat_rowid, "message_id": rowid, "message_date": date_ns})
        attachment = None
        if message.photo:
            data, _, _ = renderer.jpeg(message.photo)
            attachment_guid = _guid(rng)
            name = f"IMG_{rng.randrange(1000, 9999)}.jpeg"
            path = f"Library/SMS/Attachments/{rng.randrange(256):02x}/{rng.randrange(16):02d}/{attachment_guid}/{name}"
            attachment_rowid = db.table("attachment").insert({
                "guid": attachment_guid, "original_guid": attachment_guid,
                "created_date": int(cocoa_nanoseconds(when) // 1_000_000_000), "start_date": 0,
                "filename": f"~/{path}", "uti": "public.jpeg", "mime_type": "image/jpeg",
                "transfer_state": 5, "is_outgoing": int(from_me), "transfer_name": name, "total_bytes": len(data),
                "is_sticker": 0, "hide_attachment": 0, "ck_sync_state": 0, "is_commsafety_sensitive": 0,
            })
            db.table("message_attachment_join").insert({"message_id": rowid, "attachment_id": attachment_rowid})
            attachment = (path, name, data)
        if not message.reaction:
            latest.setdefault(chat.id, []).append((message.sender, guid, None if message.photo else text))
        sender_handle = "" if from_me else persona.contacts[message.sender].handle
        written.append(Written(guid, chat.id, identifier, when, from_me, sender_handle, "" if message.photo else text,
                               bool(message.reaction), attachment))
    for chat_id, (rowid, _, _) in chat_rows.items():
        incoming = [w for w in written if w.chat_identifier == chat_rows[chat_id][1] and not w.is_from_me]
        if incoming and chats_t.has("last_read_message_timestamp"):
            db.conn.execute("UPDATE chat SET last_read_message_timestamp = ? WHERE ROWID = ?",
                            (cocoa_nanoseconds(incoming[-1].date), rowid))
    return written

