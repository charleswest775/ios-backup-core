"""What a writer's write step works with.

Each writer module has a ``step(ctx)``: it builds its data type's database(s), adds its files to
the backup with ``ctx.writer.add_file`` and returns what its part of the answer key needs (its
module's ``answer_key``), or None when the backup holds none of that data type. generate.STEPS
runs them in order.

The steps share ``ctx.rng`` in that order, so the order of the steps, of the draws within each
and of the files each adds decides a backup's bytes. A new writer therefore goes at the end of
generate.STEPS and draws from a random generator of its own, as Calendar and Voice Memos do;
adding it to a pack then leaves the rest of that pack's backups as they were.
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
    rng: random.Random  # shared by the steps, in generate.STEPS order
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
