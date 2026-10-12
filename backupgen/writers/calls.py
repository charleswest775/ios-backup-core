"""Calls: CallHistory.storedata, a Core Data store.

Rows follow what iOS 13–17 write:
- ZCALLTYPE is 1 for a phone call, 8 for FaceTime video and 16 for FaceTime audio.
  ZSERVICE_PROVIDER is com.apple.Telephony or com.apple.FaceTime.
- ZORIGINATED is 1 for outgoing calls. ZANSWERED is 1 only on incoming calls that were
  picked up; outgoing calls keep 0 even when they connect.
- ZDATE is seconds since 2001-01-01, ZDURATION is seconds.
- ZADDRESS is the other party's number or email: a BLOB of UTF-8 before iOS 17, text after.
- Each call links its remote party in ZHANDLE through the Z_<n>REMOTEPARTICIPANTHANDLES join.
- ZCALLDBPROPERTIES keeps the phone's running call-time totals.

Apple leaves call history out of unencrypted backups, so generate.py only adds this
database to encrypted ones.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from backupgen.persona import Call, Persona, Snapshot
from backupgen.sqlitegen import SchemaDatabase
from backupgen.writers import CoreData, cocoa_seconds, country_code

_CALL_TYPES = {"phone": 1, "facetime-video": 8, "facetime-audio": 16}
_PROVIDERS = {"phone": "com.apple.Telephony", "facetime-video": "com.apple.FaceTime",
              "facetime-audio": "com.apple.FaceTime"}
_HANDLE_PHONE, _HANDLE_EMAIL = 2, 3


@dataclass
class WrittenCall:
    """One call as written, for the answer key."""

    date: datetime
    address: str
    outgoing: bool
    duration: int
    service: str


def all_calls(persona: Persona) -> list[Call]:
    """calls.txt plus the missed call each voicemail leaves behind, in time order."""
    calls = list(persona.calls or [])
    listed = {(c.who, c.real_time) for c in calls if not c.outgoing}
    for v in persona.voicemails or []:
        if (v.who, v.real_time) not in listed:
            calls.append(Call(v.real_time, False, v.who, 0, "phone", v.where))
    return sorted(calls, key=lambda c: c.real_time)


def address_of(persona: Persona, who: str, service: str = "phone") -> str:
    """The number (or, for FaceTime with someone who has no number, the email) a call shows."""
    if who.startswith("+"):
        return who
    contact = persona.contacts[who]
    if contact.phones:
        return "+" + "".join(ch for ch in contact.phones[0][1] if ch.isdigit())
    if service != "phone" and contact.emails:
        return contact.emails[0][1].lower()
    raise ValueError(f"{who} has no phone number to call")


def _uuid_blob(rng: random.Random) -> bytes:
    return uuid.UUID(bytes=rng.randbytes(16)).bytes


def populate(db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random) -> list[WrittenCall]:
    core = CoreData(db)
    records, handles_table, properties = db.table("ZCALLRECORD"), db.table("ZHANDLE"), db.table("ZCALLDBPROPERTIES")
    join_name = next((t for t in db.schema["columns"] if t.startswith("Z_") and t.endswith("REMOTEPARTICIPANTHANDLES")), None)
    join = db.table(join_name) if join_name else None
    address_is_blob = db.column_type("ZCALLRECORD", "ZADDRESS") == "BLOB"
    sim = _uuid_blob(rng)
    handles: dict[str, int] = {}
    written: list[WrittenCall] = []
    totals = {"in": 0.0, "out": 0.0}

    for call in all_calls(persona):
        when = persona.device_time(call.real_time) + timedelta(seconds=rng.randrange(30))
        if when > snapshot.backup_date:
            continue
        address = address_of(persona, call.who, call.service)
        is_email = "@" in address
        pk = records.insert({
            "Z_ENT": core.ent("CallRecord"), "Z_OPT": 1,
            "ZANSWERED": int(not call.outgoing and call.duration > 0),
            "ZCALL_CATEGORY": 2 if call.service == "facetime-video" else 1,
            "ZCALLTYPE": _CALL_TYPES[call.service],
            "ZDISCONNECTED_CAUSE": 0, "ZFILTERED_OUT_REASON": 0,
            "ZHANDLE_TYPE": _HANDLE_EMAIL if is_email else _HANDLE_PHONE,
            "ZJUNKCONFIDENCE": 0, "ZNUMBER_AVAILABILITY": 0, "ZVERIFICATIONSTATUS": 0, "ZHASMESSAGE": 0,
            "ZORIGINATED": int(call.outgoing),
            "ZREAD": 1,
            "ZDATE": cocoa_seconds(when),
            "ZDURATION": float(call.duration),
            "ZADDRESS": address.encode("utf-8") if address_is_blob else address,
            "ZISO_COUNTRY_CODE": None if is_email else country_code(address),
            "ZSERVICE_PROVIDER": _PROVIDERS[call.service],
            "ZUNIQUE_ID": str(uuid.UUID(bytes=rng.randbytes(16))).upper(),
            "ZLOCALPARTICIPANTUUID": sim,
            "ZOUTGOINGLOCALPARTICIPANTUUID": sim,
        })
        core.saw("CallRecord", pk)
        if handles_table and join:
            if address not in handles:
                handles[address] = handles_table.insert({
                    "Z_ENT": core.ent("Handle"), "Z_OPT": 1,
                    "ZTYPE": _HANDLE_EMAIL if is_email else _HANDLE_PHONE,
                    "ZNORMALIZEDVALUE": address, "ZVALUE": address,
                })
                core.saw("Handle", handles[address])
            calls_column, handles_column = (c["name"] for c in db.schema["columns"][join_name])
            join.insert({calls_column: pk, handles_column: handles[address]})
        totals["out" if call.outgoing else "in"] += call.duration
        written.append(WrittenCall(when, address, call.outgoing, call.duration, call.service))

    if properties:
        pk = properties.insert({
            "Z_ENT": core.ent("CallDBProperties"), "Z_OPT": 1,
            "ZTIMER_ALL": totals["in"] + totals["out"], "ZTIMER_INCOMING": totals["in"],
            "ZTIMER_OUTGOING": totals["out"], "ZTIMER_LIFETIME": totals["in"] + totals["out"],
            "ZTIMER_LAST": float(written[-1].duration) if written else 0.0,
        })
        core.saw("CallDBProperties", pk)
    core.finish()
    return written


def contact_name(cards: list, address: str) -> Optional[str]:
    """The name a reader should show for an address, from the cards in this backup."""
    digits = "".join(ch for ch in address if ch.isdigit())
    for card in cards:
        if any(address == e.lower() for _, e in card.emails):
            return card.display_name
        if digits and any("".join(ch for ch in n if ch.isdigit()) == digits for _, n in card.phones):
            return card.display_name
    return None
