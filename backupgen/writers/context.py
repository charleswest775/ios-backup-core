"""What a writer's write step works with.

Each writer module has a ``step(ctx)``: it builds its data type's database(s), adds its files to
the backup with ``ctx.writer.add_file`` and returns what its part of the answer key needs (its
module's ``answer_key``), or None when the backup holds none of that data type. generate.STEPS
runs them in order.

Random numbers: each step gets a context whose ``ctx.rng`` is its own (generate.area_random,
made from the snapshot's seed and the step's data type) and draws only from that, so no step's
draws move another's, and adding to one data type in a pack changes no other data type's
databases, files or answer key. Within a step, the order of its draws still decides its
contents. A new writer follows the same rule (see generate.STEPS).

The container (BackupWriter: keybag, file keys, inode numbers, Status.plist) has a generator
of its own, as before, drawn in file order: adding a file renumbers the inodes in Manifest.db
and, in an encrypted backup, gives the files after it new keys, but what every other file
decrypts to stays the same.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from backupgen.sqlitegen import SchemaDatabase, build

if TYPE_CHECKING:
    from backupgen.container import BackupWriter
    from backupgen.media import Renderer
    from backupgen.persona.core import Persona, Snapshot


@dataclass
class StepContext:
    persona: Persona
    snapshot: Snapshot
    rng: random.Random  # this step's own random numbers (generate.area_random); draw only from these
    writer: BackupWriter  # the backup being written
    renderer: Renderer  # each persona photo as a camera JPEG, for Photos, Messages and WhatsApp
    tmp: Path  # a scratch folder for building databases
    # What one step leaves for a later one: the contacts step's contact id -> ABPerson guid, which
    # Photos uses to link People album groups to cards (empty where the schema has no guid column).
    contact_guids: dict[str, Optional[str]] = field(default_factory=dict)

    @property
    def encrypted(self) -> bool:
        return self.writer.encrypted

    def build(self, database: str, populate: Callable[[SchemaDatabase], None]) -> bytes:
        """Build ``database`` (e.g. "sms.db") from the snapshot's harvested schema; returns its bytes."""
        return build(self.tmp / database, self.snapshot.schema, database, populate)
