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

from backupgen import media
from backupgen.container import BackupWriter, Device
from backupgen.persona import Persona, load_persona
from backupgen.writers import calendar as calendar_writer
from backupgen.writers import calls as calls_writer
from backupgen.writers import contacts as contacts_writer
from backupgen.writers import messages as messages_writer
from backupgen.writers import notes as notes_writer
from backupgen.writers import photos as photos_writer
from backupgen.writers import safari as safari_writer
from backupgen.writers import voice_memos as voice_memos_writer
from backupgen.writers import voicemail as voicemail_writer
from backupgen.writers import whatsapp as whatsapp_writer
from backupgen.writers.context import StepContext

EXPECTED = "expected.json"

# Every data type OpenExtract reads or is adding. The answer key lists the ones a backup doesn't hold
# under "absent", with why, so readers can be checked for clean empty results as well as for content:
# "none" means the phone has none; "encrypted_only" means Apple leaves it out of unencrypted backups.
DATA_TYPES = ("messages", "contacts", "calls", "voicemail", "notes", "photos", "browser_history",
              "calendar", "voice_memos", "health", "whatsapp")
ENCRYPTED_ONLY = {"calls", "browser_history", "health"}

# The write steps, in the order they run (see writers/context.py). Each builds its data type's
# database(s), adds its files to the backup and returns what its part of the answer key needs,
# kept under its data type (None: the backup holds none).
#
# Random numbers: each step draws only from its own stream, ctx.rng = area_random(seed, <its data
# type>), never from another step's or a shared one, so adding a message, a call or a photo to a
# pack changes only that data type's databases, files and answer key. Every new step follows the
# same rule: it draws only from its ctx.rng, and anything one step leaves for another
# (ctx.contact_guids) is data, not random numbers. Calendar and Voice Memos keep the fixed seeds
# they had before this rule ("<persona>/<snapshot>/calendar" and ".../voice_memos", which don't
# follow --seed), as do Photos' People album and each deleted note, so their data stayed as it was.
STEPS = (
    ("messages", messages_writer.step),
    ("contacts", contacts_writer.step),
    ("calls", calls_writer.step),
    ("voicemail", voicemail_writer.step),
    ("notes", notes_writer.step),
    ("browser_history", safari_writer.step),
    ("photos", photos_writer.step),
    ("whatsapp", whatsapp_writer.step),
    ("calendar", calendar_writer.step),
    ("voice_memos", voice_memos_writer.step),
)


def default_seed(persona_id: str, snapshot_id: str, encrypted: bool) -> int:
    digest = hashlib.sha256(f"{persona_id}/{snapshot_id}/{'enc' if encrypted else 'plain'}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def area_random(seed: int, area: str) -> random.Random:
    """The random numbers of one data type ("area", e.g. "messages") in the snapshot with this seed.

    Made from the seed and the area's name only, so no area's draws depend on another's, and,
    like the seed, they differ between plain and encrypted backups and follow --seed. A string
    seed is hashed with SHA-512 by ``random``, never with hash(), so they are the same in every
    process whatever PYTHONHASHSEED is.
    """
    return random.Random(f"backupgen/{seed}/{area}")


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
    rng = random.Random(seed)  # only the container's seed comes from here; each step has its own (area_random)
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
        renderer, contact_guids = media.Renderer(persona, snapshot), {}
        written = {}
        for kind, step in STEPS:
            # What the steps share, with this step's own random numbers.
            ctx = StepContext(persona, snapshot, area_random(seed, kind), writer, renderer, Path(tmp), contact_guids)
            result = step(ctx)
            if result is not None:
                written[kind] = result

    backup_dir = writer.finalize()

    expected = answer_key(persona, snapshot_id, device, password, written)
    (out_dir / EXPECTED).write_text(json.dumps(expected, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return backup_dir


def answer_key(persona: Persona, snapshot_id: str, device: Device, password: Optional[str], written: dict) -> dict:
    """The answer key: the backup's identity, then each writer's part (see its answer_key) in a fixed order."""
    snapshot = persona.snapshots[snapshot_id]
    cards = written["contacts"]
    present = {kind for kind, result in written.items() if result}
    # A written Calendar.sqlitedb or CloudRecordings.db is there even with nothing in it, as on a phone:
    # the readers then list the calendars, or answer "no_records", not "not_in_backup".
    present |= {"calendar", "voice_memos"} & written.keys()
    on_phone = {"calls": bool(persona.calls or persona.voicemails), "browser_history": bool(persona.visits)}
    left_out = {t for t in ENCRYPTED_ONLY if password is None and on_phone.get(t)}
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
        "contacts": contacts_writer.answer_key(persona, cards),
        "conversations": messages_writer.conversations_answer_key(persona, written["messages"]),
        "messages": messages_writer.answer_key(written["messages"]),
        "calls": calls_writer.answer_key(written.get("calls"), cards),
        "voicemails": voicemail_writer.answer_key(written.get("voicemail"), cards),
        "notes": notes_writer.answer_key(written.get("notes")),
        "browser_history": safari_writer.answer_key(written.get("browser_history")),
        "photos": photos_writer.answer_key(written.get("photos")),
        # The People album (see writers/photos.py): every group, with the photos a reader should list for it.
        "photo_people": photos_writer.people_answer_key(persona, written.get("photos")),
        # Chats in arrival order (each message's "sort"); see writers/whatsapp.py. None when the phone has no WhatsApp.
        "whatsapp": whatsapp_writer.answer_key(written.get("whatsapp")),
        # Shaped like the readers' records (see writers/calendar.py and writers/voice_memos.py); events in
        # row order, recordings newest first. None when the pack has no calendar.json / voice_memos.txt.
        "calendar": calendar_writer.answer_key(written.get("calendar")),
        "voice_memos": voice_memos_writer.answer_key(written.get("voice_memos")),
        "absent": {t: "encrypted_only" if t in left_out else "none" for t in DATA_TYPES if t not in present},
    }
