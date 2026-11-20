"""Voice Memos: CloudRecordings.db and the recordings beside it.

Where they live depends on the iOS version: in the app group
``AppDomainGroup-group.com.apple.VoiceMemos.shared`` under ``Recordings/`` (iOS 17 on; the
move is thought to have come with iOS 17, which no public backup confirms), and before that
in ``MediaDomain`` under ``Media/Recordings/`` (seen on 13.3.1 and 13.4.1). The database
follows the harvested schema for the snapshot: Apple's VoiceMemos14 model for 26.5 (an empty
store made by Core Data from the Simulator's framework, see ``tools/coredata_store.swift``),
otherwise the iOS 13 layout, the only one seen in a real backup. Models 7 to 13 (iOS 14 to
18) were never harvested, so a 15.3.1 or 17.3 snapshot gets the iOS 13 columns.

Rows (ZCLOUDRECORDING), as the framework reads them:

- ZDATE is when recording started (Cocoa seconds); ZDURATION is the audio's length, and
  ZLOCALDURATION (newer layouts) the local file's, 0 when the audio isn't on the phone.
- The title goes in ZCUSTOMLABEL and, where the columns exist, ZENCRYPTEDTITLE (plain text
  in the local store; only the iCloud copy is encrypted) and ZCUSTOMLABELFORSORTING.
- ZPATH is the file name, ``YYYYMMDD HHMMSS-<first 8 of ZUNIQUEID>.m4a`` in the phone's local
  time (the year is ICU's week year ``Y``, as Apple's formatter writes it, so a recording made
  on 29 to 31 December can carry the next year). It is NULL for a recording kept in iCloud
  only: the path is local and never synced.
- ZFLAGS: 0x4 playable (the audio is here), 0x2 instead for a recording whose audio is only in
  iCloud (no file is written), 0x200 | 0x400 for a transcribed recording with text (iOS 18 on;
  the transcript lives in the file's ``tsrp`` box, never in the database).
- ZSHAREDFLAGS 0x1 marks a favourite (newer layouts only; the iOS 13 layout has no favourites
  or folders). ZEVICTIONDATE is when a recording went to Recently Deleted: a day after it was
  made, or just before the backup if that is sooner. One deleted more than 30 days before the
  backup is gone for good (Apple's default), so it isn't written.
- ZFOLDER rows (newer layouts): ZUUID, ZENCRYPTEDNAME, ZRANK, and ZCOUNTOFRECORDINGS, which
  Core Data's triggers keep as the number of rows in the folder, Recently Deleted included.
  Triggers aren't run while generating, so it is written directly.

Audio is silent AAC (``media.silent_m4a``), tagged with the title, the Voice Memos encoder
string and the recording's unique id. Not written: waveform files, ``.composition`` folders
(only edited recordings have them), layered or spatial ``.qta`` recordings, and the Core Data
history tables' rows. The flag bits and the file naming come from the iOS 26.5 framework and
have not been checked against a real backup yet.
"""

from __future__ import annotations

import random
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from backupgen.media import m4a_duration, silent_m4a
from backupgen.persona import Persona, Snapshot, VoiceMemoDef
from backupgen.sqlitegen import build, load_schema
from backupgen.writers import CoreData, cocoa_seconds

SHARED_DOMAIN = "AppDomainGroup-group.com.apple.VoiceMemos.shared"
LEGACY_DOMAIN = "MediaDomain"
DATABASE = "CloudRecordings.db"
PLAYABLE, NEEDS_DOWNLOAD, TRANSCRIBED, HAS_TRANSCRIPT = 0x4, 0x2, 0x200, 0x400
FAVORITE = 0x1
RECENTLY_DELETED_DAYS = 30
TRANSCRIPTS_FROM = 18  # iOS major version


@dataclass
class Store:
    """What write() produced: the files to add to the backup and the recordings they hold."""

    domain: str
    folder: str  # the database's folder in that domain: "Recordings" or "Media/Recordings"
    schema: str  # the harvested CloudRecordings.db layout used
    files: dict = field(default_factory=dict)  # (domain, relative path) -> bytes
    memos: list = field(default_factory=list)  # answer-key records, newest first


def location(snapshot: Snapshot) -> tuple[str, str]:
    """(domain, folder) of the database and recordings for this iOS version."""
    if int(snapshot.ios.split(".")[0]) >= 17:
        return SHARED_DOMAIN, "Recordings"
    return LEGACY_DOMAIN, "Media/Recordings"


def week_year(day: date) -> int:
    """ICU's ``Y`` for en_US (weeks start on Sunday; the week holding 1 January is week 1)."""
    saturday = day + timedelta(days=(5 - day.weekday()) % 7)
    return saturday.year


def file_name(local: datetime, unique_id: str) -> str:
    return f"{week_year(local.date())}{local:%m%d %H%M%S}-{unique_id[:8]}.m4a"


def transcript_segments(text: str, seconds: float) -> list[tuple[str, float, float]]:
    """Words of ``text`` spread over the recording, as Voice Memos' runs: words after the first keep their space."""
    words = re.findall(r"\S+", text)
    if not words:
        return []
    step = min(0.45, max(seconds - 0.5, 0.1) / len(words))
    return [((" " if n else "") + word, round(0.5 + n * step, 2), round(0.5 + (n + 1) * step, 2))
            for n, word in enumerate(words)]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class _Writer:
    def __init__(self, persona: Persona, snapshot: Snapshot, rng: random.Random):
        self.persona, self.snapshot, self.rng = persona, snapshot, rng
        self.domain, self.folder = location(snapshot)
        self.schema = load_schema(snapshot.schema, DATABASE)["ios"]
        self.transcripts = int(snapshot.ios.split(".")[0]) >= TRANSCRIPTS_FROM
        self.store = Store(self.domain, self.folder, self.schema)

    def populate(self, db) -> None:
        core = CoreData(db)
        recordings, folders = db.table("ZCLOUDRECORDING"), db.table("ZFOLDER")
        backup = self.snapshot.backup_date
        encoder = f"com.apple.VoiceMemos (iPhone Version {self.snapshot.ios} (Build {self.snapshot.build}))"
        folder_rows: dict[str, dict] = {}
        pk = 0
        for memo in sorted(self.persona.voice_memos or [], key=lambda m: m.real_time):
            when = self.persona.device_time(memo.real_time) + timedelta(seconds=self.rng.randrange(60))
            unique_id = str(uuid.UUID(bytes=self.rng.randbytes(16), version=4)).upper()
            if when > backup:
                continue
            deleted = None
            if memo.deleted:
                deleted = min(when + timedelta(days=1), backup - timedelta(minutes=10))
                if deleted <= when:
                    deleted = when + timedelta(seconds=memo.duration)
                if deleted < backup - timedelta(days=RECENTLY_DELETED_DAYS):
                    continue  # erased from Recently Deleted before the backup
            pk += 1
            folder = None
            if memo.folder and folders is not None:
                if memo.folder not in folder_rows:
                    folder_rows[memo.folder] = {"pk": len(folder_rows) + 1, "rank": len(folder_rows),
                                                "uuid": str(uuid.UUID(bytes=self.rng.randbytes(16), version=4)).upper(),
                                                "count": 0}
                folder = folder_rows[memo.folder]
                folder["count"] += 1
            self.write_one(recordings, core, pk, memo, when, unique_id, deleted, folder, encoder)
        if folders is not None:
            for name, f in folder_rows.items():
                folders.insert({"Z_PK": f["pk"], "Z_ENT": core.ent("Folder"), "Z_OPT": 1, "ZCOUNTOFRECORDINGS": f["count"],
                                "ZRANK": f["rank"], "ZENCRYPTEDNAME": name, "ZUUID": f["uuid"]})
                core.saw("Folder", f["pk"])
        core.saw("CloudRecording", pk)
        core.finish()
        self.store.memos.sort(key=lambda m: (m["date"], m["id"]), reverse=True)

    def write_one(self, recordings, core, pk: int, memo: VoiceMemoDef, when: datetime, unique_id: str,
                  deleted, folder, encoder: str) -> None:
        duration = m4a_duration(memo.duration)
        local = when.astimezone(self.persona.tz)
        name = None if memo.icloud else file_name(local, unique_id)
        transcribed = bool(memo.transcript) and self.transcripts
        flags = (NEEDS_DOWNLOAD if memo.icloud else PLAYABLE) | (TRANSCRIBED | HAS_TRANSCRIPT if transcribed else 0)
        recordings.insert({
            "Z_PK": pk, "Z_ENT": core.ent("CloudRecording"), "Z_OPT": 1, "ZFLAGS": flags,
            "ZSHAREDFLAGS": FAVORITE if memo.favorite else 0, "ZAUDIOFUTUREFLAGS": 0, "ZSILENCEREMOVERENABLED": 0,
            "ZITUNESPERSISTENTID": 0, "ZFOLDER": folder["pk"] if folder else None, "ZDATE": cocoa_seconds(when),
            "ZDURATION": duration, "ZEVICTIONDATE": cocoa_seconds(deleted) if deleted else None,
            "ZLOCALDURATION": 0.0 if memo.icloud else duration, "ZPLAYBACKPOSITION": 0.0, "ZPLAYBACKRATE": 1.0,
            "ZCUSTOMLABEL": memo.title, "ZCUSTOMLABELFORSORTING": memo.title, "ZENCRYPTEDTITLE": memo.title,
            "ZPATH": name, "ZUNIQUEID": unique_id,
        })
        data = None
        if name:
            data = silent_m4a(memo.duration, int(when.timestamp()), title=memo.title, encoder=encoder,
                              unique_id=unique_id,
                              transcript=transcript_segments(memo.transcript, duration) if transcribed else None)
            self.store.files[(self.domain, f"{self.folder}/{name}")] = data
        self.store.memos.append({
            "id": pk, "unique_id": unique_id, "title": memo.title, "date": _iso(when),
            "duration_seconds": duration, "folder": folder and memo.folder, "folder_id": folder and folder["uuid"],
            "is_favorite": memo.favorite if recordings.has("ZSHAREDFLAGS") else None,
            "is_deleted": deleted is not None, "deleted_date": _iso(deleted) if deleted else None,
            "in_phone_list": True, "recorded_on_watch": False,
            "is_enhanced": False if recordings.has("ZSHAREDFLAGS") else None, "is_layered": False,
            "is_spatial": False, "has_transcript": transcribed, "transcript": memo.transcript if transcribed else None,
            "file_name": name, "format": "m4a" if name else None, "audio_available": name is not None,
            "audio_missing_reason": "in_icloud_only" if memo.icloud else None,
            "bytes": len(data) if data else None, "source": DATABASE,
        })


def write(tmp: Path, persona: Persona, snapshot: Snapshot, rng: random.Random) -> Store:
    """Build Voice Memos' database and recordings for this snapshot. Returns them with what they hold."""
    writer = _Writer(persona, snapshot, rng)
    store = writer.store
    data = build(Path(tmp) / DATABASE, snapshot.schema, DATABASE, writer.populate)
    store.files = {(writer.domain, f"{writer.folder}/{DATABASE}"): data, **store.files}
    return store
