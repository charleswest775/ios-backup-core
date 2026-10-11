"""Generate one backup snapshot of a persona, plus its answer key.

Output layout:

    <out>/<UDID>/          the backup folder (Manifest.db, Manifest.plist, Info.plist, Status.plist, xx/<fileID>)
    <out>/expected.json    what a correct reader should extract

The answer key comes from the persona pack, not from any reader, so tests that
compare against it are independent of the code under test.
"""

from __future__ import annotations

import hashlib
import json
import random
import tempfile
from pathlib import Path
from typing import Optional

from backupgen.container import BackupWriter, Device
from backupgen.persona import Persona, load_persona
from backupgen.sqlitegen import build
from backupgen.writers import contacts as contacts_writer
from backupgen.writers import messages as messages_writer

EXPECTED = "expected.json"

# Every data type OpenExtract reads or is adding. The answer key lists the ones a backup doesn't hold
# under "absent", so readers can be checked for clean empty results as well as for content.
DATA_TYPES = ("messages", "contacts", "calls", "voicemail", "notes", "photos", "browser_history",
              "calendar", "voice_memos", "health")


def default_seed(persona_id: str, snapshot_id: str, encrypted: bool) -> int:
    digest = hashlib.sha256(f"{persona_id}/{snapshot_id}/{'enc' if encrypted else 'plain'}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def generate(
    persona_id: str,
    snapshot_id: str,
    out_dir: Path,
    *,
    password: Optional[str] = None,
    iterations: str = "fast",
    seed: Optional[int] = None,
    persona: Optional[Persona] = None,
) -> Path:
    """Write the backup for one snapshot into ``out_dir``. Returns the backup folder."""
    persona = persona or load_persona(persona_id)
    snapshot = persona.snapshots[snapshot_id]
    seed = default_seed(persona.id, snapshot_id, password is not None) if seed is None else seed
    rng = random.Random(seed)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    device = Device(
        name=persona.device_name,
        product_type=snapshot.product_type,
        product_name=snapshot.product_name,
        ios=snapshot.ios,
        build=snapshot.build,
        phone_number=persona.owner.phones[0][1] if persona.owner.phones else "",
    )
    # The same phone keeps its UDID, serial and IMEI across snapshots and encryption settings.
    identity = hashlib.sha256(f"{persona.id}/{snapshot.product_type}".encode()).digest()
    device.fill_identifiers(random.Random(int.from_bytes(identity[:8], "big")))
    writer = BackupWriter(out_dir, device, snapshot.backup_date, snapshot.container,
                          password=password, iterations=iterations, seed=rng.getrandbits(64), host=snapshot.host)

    with tempfile.TemporaryDirectory(prefix="backupgen-") as tmp:
        tmp = Path(tmp)
        written_messages: list = []
        contact_rows: dict = {}

        def fill_messages(db):
            written_messages.extend(messages_writer.populate(db, persona, snapshot, rng))

        def fill_contacts(db):
            contact_rows.update(contacts_writer.populate(db, persona, snapshot, rng))

        sms = build(tmp / "sms.db", snapshot.schema, "sms.db", fill_messages)
        address_book = build(tmp / "AddressBook.sqlitedb", snapshot.schema, "AddressBook.sqlitedb", fill_contacts)
        images = build(tmp / "AddressBookImages.sqlitedb", snapshot.schema, "AddressBookImages.sqlitedb", lambda db: None)

    writer.add_file("HomeDomain", "Library/SMS/sms.db", sms)
    writer.add_file("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb", address_book)
    writer.add_file("HomeDomain", "Library/AddressBook/AddressBookImages.sqlitedb", images)
    backup_dir = writer.finalize()

    expected = answer_key(persona, snapshot_id, device, password, written_messages)
    (out_dir / EXPECTED).write_text(json.dumps(expected, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return backup_dir


def answer_key(persona: Persona, snapshot_id: str, device: Device, password: Optional[str], written: list) -> dict:
    snapshot = persona.snapshots[snapshot_id]
    cards = contacts_writer.visible_contacts(persona, snapshot)
    chats = {chat.id: chat for chat in persona.chats}
    conversations: dict[str, dict] = {}
    for w in written:
        chat = chats[w.chat_id]
        conv = conversations.setdefault(w.chat_id, {
            "chat": chat.id,
            "chat_identifier": w.chat_identifier,
            "service": chat.service,
            "is_group": chat.is_group,
            "name": chat.name if chat.is_group else "",
            "participants": [persona.contacts[p].handle for p in chat.participants],
            "message_count": 0,
        })
        conv["message_count"] += 1
        conv["last_message_date"] = w.date.isoformat()
    present = {"messages"} if written else set()
    if cards:
        present.add("contacts")
    return {
        "persona": persona.id,
        "snapshot": snapshot_id,
        "ios": snapshot.ios,
        "build": snapshot.build,
        "product_type": snapshot.product_type,
        "udid": device.udid,
        "backup_date": snapshot.backup_date.isoformat(),
        "encrypted": password is not None,
        "password": password,
        "contacts": [
            {
                "first": c.first, "last": c.last, "organization": c.organization,
                "phones": [number for _, number in c.phones], "emails": [address for _, address in c.emails],
                "is_owner": c is persona.owner,
            }
            for c in cards
        ],
        "conversations": sorted(conversations.values(), key=lambda c: c["chat_identifier"]),
        "messages": [
            {
                "guid": w.guid, "chat_identifier": w.chat_identifier, "date": w.date.isoformat(),
                "is_from_me": w.is_from_me, "sender_handle": w.sender_handle, "text": w.text, "is_reaction": w.is_reaction,
            }
            for w in written
        ],
        "absent": [t for t in DATA_TYPES if t not in present],
    }
