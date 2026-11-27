"""What a writer declares (Step) and what its write step works with (StepContext).

Each writer module has a ``step(ctx)``: it builds its data type's database(s), adds its files to
the backup with ``ctx.writer.add_file`` and returns what its part of the answer key needs. Its
module's ``STEP`` declares the data type around that function (see Step); generate.STEPS lists
the writers' STEPs and runs them in order, and generate.answer_key is built from them.

What a step returns: None, or an empty collection, means the backup holds none of that data type
(it's listed under "absent"), unless the Step says present_if_written (Calendar and Voice Memos:
an empty database is still there). A step that returns None isn't in ``written`` at all, so
``sections`` functions read results with ``written.get(kind)``.

Random numbers: each step gets a context whose ``ctx.rng`` is its own (generate.area_random,
made from the snapshot's seed and the step's data type) and draws only from that, so no step's
draws move another's, and adding to one data type in a pack changes the contents of no other
data type's databases, files or answer key. Within a step, the order of its draws still decides
its contents. A new writer follows the same rule (see generate.STEPS), and anything one step
leaves for another (ctx.contact_guids) is data from the pack, not random numbers.

The container (BackupWriter: keybag, file keys, inode numbers, Status.plist) has a generator
of its own, as before, drawn in file order: adding a file renumbers the inodes in Manifest.db
and, in an encrypted backup, gives the files after it new keys, but what every other file
decrypts to stays the same.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from backupgen.sqlitegen import SchemaDatabase, build

if TYPE_CHECKING:
    from backupgen.container import BackupWriter
    from backupgen.media import Renderer
    from backupgen.persona.core import Persona, Snapshot


@dataclass
class StepContext:
    kind: str  # the step's data type (Step.kind)
    persona: Persona
    snapshot: Snapshot
    rng: random.Random  # this step's own random numbers (generate.area_random); draw only from these
    writer: BackupWriter  # the backup being written
    renderer: Renderer  # each persona photo as a camera JPEG, for Photos, Messages and WhatsApp
    tmp: Path  # a scratch folder for building databases
    # What one step leaves for a later one: the contacts step's contact id -> ABPerson guid for
    # every card it wrote (None where the schema has no guid column), which Photos uses to link
    # People album groups to cards. Empty until the contacts step has run.
    contact_guids: dict[str, Optional[str]] = field(default_factory=dict)

    @property
    def encrypted(self) -> bool:
        return self.writer.encrypted

    def build(self, database: str, populate: Callable[[SchemaDatabase], None]) -> bytes:
        """Build ``database`` (e.g. "sms.db") from the snapshot's harvested schema; returns its bytes."""
        return build(self.tmp / database, self.snapshot.schema, database, populate)


@dataclass(frozen=True)
class Step:
    """One data type's writer, as its module declares it (``STEP = Step(...)``) for generate.STEPS.

    ``kind`` is the data type's name ("messages", "browser_history", ...). It names the step's
    random numbers (generate.area_random), its result in ``written`` and its entry under
    "absent" in the answer key, so it must never be renamed: that would reseed the data type's
    generated data and break answer-key checks of "absent".
    """

    kind: str
    run: Callable[[StepContext], Any]  # step(ctx): writes the data type; returns its result (see the module docs)
    # (persona, written) -> {answer-key section: value}, in the order they go in the answer key.
    # ``written`` holds every step's result by kind, so a section may use another's (the calls
    # step names callers from the contacts step's cards); a step that returned None isn't in it.
    sections: Callable[[Persona, Mapping[str, Any]], dict]
    encrypted_only: bool = False  # Apple leaves it out of unencrypted backups
    # For an encrypted-only data type: whether the phone has any, so an unencrypted backup lists
    # it under "absent" as "encrypted_only" rather than "none".
    on_phone: Optional[Callable[[Persona], bool]] = None
    present_if_written: bool = False  # an empty result still counts as there (not "absent")
