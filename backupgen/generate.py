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

# The write steps, in the order they run: each writer module's STEP (see writers/context.py, Step).
# A new writer is its module's STEP plus one line here (and its import above); its answer-key
# sections go after the ones before it, and its data type joins DATA_TYPES. A Step's kind names its
# random numbers, its result and its "absent" entry, so a kind is never renamed.
#
# Random numbers: each step draws only from its own stream, ctx.rng = area_random(seed, <its data
# type>), never from another step's or a shared one, so adding a message, a call or a photo to a
# pack changes only the contents of that data type's databases, files and answer key. Every new
# step follows the same rule: it draws only from its ctx.rng, and anything one step leaves for
# another (ctx.contact_guids) is data, not random numbers. Calendar and Voice Memos keep the fixed
# seeds they had before this rule ("<persona>/<snapshot>/calendar" and ".../voice_memos", which
# don't follow --seed), as do Photos' People album and each deleted note, so their data stayed as
# it was. So the order of the steps changes no data, only the order of the answer key's sections.
STEPS = (
    contacts_writer.STEP,  # first: later steps use its cards (calls, voicemail) and their guids (photos)
    messages_writer.STEP,
    calls_writer.STEP,
    voicemail_writer.STEP,
    notes_writer.STEP,
    safari_writer.STEP,
    photos_writer.STEP,
    whatsapp_writer.STEP,
    calendar_writer.STEP,
    voice_memos_writer.STEP,
)

# Data types OpenExtract reads or is adding that no writer writes yet, with whether Apple leaves
# them out of unencrypted backups: always "absent" ("none"). A writer for one takes it out of here.
PLANNED = {"health": True}

# The order of the answer key's "absent" entries from when DATA_TYPES was written by hand, kept so
# answer keys stay byte for byte as they were; a data type not named here follows, in STEPS order.
_ABSENT_ORDER = ("messages", "contacts", "calls", "voicemail", "notes", "photos", "browser_history",
                 "calendar", "voice_memos", "health", "whatsapp")


def data_types(steps=None) -> tuple[str, ...]:
    """Every data type OpenExtract reads or is adding, in the order the answer key's "absent" lists them.

    The answer key lists the ones a backup doesn't hold under "absent", with why, so readers can be
    checked for clean empty results as well as for content: "none" means the phone has none;
    "encrypted_only" means Apple leaves it out of unencrypted backups.
    """
    kinds = [step.kind for step in (STEPS if steps is None else steps)] + list(PLANNED)
    return tuple([k for k in _ABSENT_ORDER if k in kinds] + [k for k in kinds if k not in _ABSENT_ORDER])


DATA_TYPES = data_types()
ENCRYPTED_ONLY = {step.kind for step in STEPS if step.encrypted_only} | {k for k, only in PLANNED.items() if only}


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
        for step in STEPS:
            # What the steps share, with this step's own random numbers.
            ctx = StepContext(step.kind, persona, snapshot, area_random(seed, step.kind), writer, renderer, Path(tmp),
                              contact_guids)
            result = step.run(ctx)
            if result is not None:
                written[step.kind] = result

    backup_dir = writer.finalize()

    expected = answer_key(persona, snapshot_id, device, password, written)
    (out_dir / EXPECTED).write_text(json.dumps(expected, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return backup_dir


def answer_key(persona: Persona, snapshot_id: str, device: Device, password: Optional[str], written: dict) -> dict:
    """The answer key: the backup's identity, then each step's sections (its Step.sections) in STEPS order,
    then "absent"."""
    snapshot = persona.snapshots[snapshot_id]
    key = {
        "persona": persona.id,
        "snapshot": snapshot_id,
        "ios": snapshot.ios,
        "build": snapshot.build,
        "product_type": snapshot.product_type,
        "udid": device.udid,
        "backup_date": snapshot.backup_date.isoformat(),
        "encrypted": password is not None,
        "password": password,
    }
    for step in STEPS:
        for section, value in step.sections(persona, written).items():
            if section in key or section == "absent":
                raise ValueError(f"{step.kind}: answer-key section {section!r} is already taken")
            key[section] = value
    present = {step.kind for step in STEPS
               if step.kind in written and (written[step.kind] or step.present_if_written)}
    left_out = {step.kind for step in STEPS
                if step.encrypted_only and password is None and step.on_phone is not None and step.on_phone(persona)}
    key["absent"] = {t: "encrypted_only" if t in left_out else "none" for t in data_types() if t not in present}
    return key
