"""Safari: Library/Safari/History.db.

Rows follow what iOS 15–26 write:
- history_items has one row per URL with its visit_count; history_visits has one row per
  visit, with visit_time in seconds since 2001-01-01 and the page's title at the time.
- domain_expansion is the host without "www." and its top-level domain ("nps",
  "en.wikipedia"). It's a label, not a host.
- daily_visit_counts is a packed array Safari keeps for ranking. Readers don't use it,
  so it holds just the item's total.
- origin 0 marks visits made on this device rather than synced from another one.

Apple leaves website history out of unencrypted backups, so generate.py only adds this
database to encrypted ones.
"""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

from backupgen.persona import Persona, Snapshot
from backupgen.sqlitegen import SchemaDatabase
from backupgen.writers import cocoa_seconds


@dataclass
class WrittenVisit:
    """One visit as written, for the answer key."""

    date: datetime
    url: str
    title: str


def domain_expansion(url: str) -> str:
    host = urlparse(url).hostname or ""
    host = host.removeprefix("www.")
    return host.rsplit(".", 1)[0] if "." in host else host


def populate(db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random) -> list[WrittenVisit]:
    items, visits = db.table("history_items"), db.table("history_visits")
    planned = []
    for visit in sorted(persona.visits or [], key=lambda v: v.real_time):
        when = persona.device_time(visit.real_time) + timedelta(seconds=rng.randrange(60), microseconds=rng.randrange(10**6))
        if when <= snapshot.backup_date:
            planned.append((when, visit))
    counts: dict[str, int] = {}
    for _, visit in planned:
        counts[visit.url] = counts.get(visit.url, 0) + 1
    item_ids: dict[str, int] = {}
    written = []
    for when, visit in planned:
        if visit.url not in item_ids:
            count = counts[visit.url]
            item_ids[visit.url] = items.insert({
                "url": visit.url,
                "domain_expansion": domain_expansion(visit.url),
                "visit_count": count,
                "daily_visit_counts": struct.pack("<i", count),
                "should_recompute_derived_visit_counts": 0,
                "visit_count_score": 100 * count,
                "status_code": 0,
            })
        visits.insert({
            "history_item": item_ids[visit.url],
            "visit_time": cocoa_seconds(when),
            "title": visit.title,
            "load_successful": 1, "http_non_get": 0, "synthesized": 0,
            "origin": 0, "generation": 0, "attributes": 0, "score": 100,
        })
        written.append(WrittenVisit(when, visit.url, visit.title))
    return written
