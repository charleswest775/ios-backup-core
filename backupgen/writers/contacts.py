"""Contacts: AddressBook.sqlitedb (and an empty AddressBookImages.sqlitedb)."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, time

from backupgen.persona import Contact, Persona, Snapshot
from backupgen.sqlitegen import SchemaDatabase
from backupgen.writers import cocoa_seconds

PHONE, EMAIL = 3, 4
LABELS = {
    "mobile": "_$!<Mobile>!$_",
    "home": "_$!<Home>!$_",
    "work": "_$!<Work>!$_",
    "main": "_$!<Main>!$_",
    "other": "_$!<Other>!$_",
    "iphone": "iPhone",
}


def _guid(rng: random.Random) -> str:
    return str(uuid.UUID(bytes=rng.randbytes(16))).upper()


def visible_contacts(persona: Persona, snapshot: Snapshot) -> list[Contact]:
    """The owner's card plus every contact added before the backup was made."""
    cards = [persona.owner]
    for contact in persona.contacts.values():
        added = contact.added or persona.real_start
        if persona.device_time(datetime.combine(added, time(9, 0))) <= snapshot.backup_date:
            cards.append(contact)
    return cards


def populate(db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random) -> dict[str, int]:
    """Write the cards; returns contact id → ABPerson ROWID."""
    people, values, labels = db.table("ABPerson"), db.table("ABMultiValue"), db.table("ABMultiValueLabel")
    store = db.table("ABStore")
    label_ids = {key: labels.insert({"value": value}) for key, value in LABELS.items()}
    owner_rowid = None
    rowids: dict[str, int] = {}
    store_id = None
    if store:
        store_id = store.insert({
            "Name": "Local", "Type": 0, "Enabled": 1, "guid": _guid(rng),
            "ExternalIdentifier": None, "Capabilities": 0, "AccountID": 0,
        })
    for contact in visible_contacts(persona, snapshot):
        added = contact.added or persona.real_start
        created = cocoa_seconds(persona.device_time(datetime.combine(added, time(9, rng.randrange(60)))))
        rowid = people.insert({
            "First": contact.first or None,
            "Last": contact.last or None,
            "Nickname": contact.nickname or None,
            "Organization": contact.organization or None,
            "Department": contact.department or None,
            "JobTitle": contact.job_title or None,
            "Note": contact.note or None,
            "Kind": 0,
            "FirstSort": f"{contact.first} {contact.last}".strip() or None,
            "LastSort": f"{contact.last} {contact.first}".strip() or None,
            "FirstSortSection": (contact.first or contact.last or "#")[:1].upper(),
            "LastSortSection": (contact.last or contact.first or "#")[:1].upper(),
            "FirstSortLanguageIndex": 0,
            "LastSortLanguageIndex": 0,
            "CreationDate": created,
            "ModificationDate": created,
            "StoreID": store_id,
            "PersonLink": -1,
            "IsPreferredName": 1,
            "guid": _guid(rng),
        })
        rowids[contact.id] = rowid
        if contact is persona.owner:
            owner_rowid = rowid
        for identifier, (label, number) in enumerate(contact.phones):
            values.insert({"record_id": rowid, "property": PHONE, "identifier": identifier,
                           "label": label_ids.get(label, label_ids["other"]), "value": number, "guid": _guid(rng)})
        for identifier, (label, address) in enumerate(contact.emails, start=len(contact.phones)):
            values.insert({"record_id": rowid, "property": EMAIL, "identifier": identifier,
                           "label": label_ids.get(label, label_ids["other"]), "value": address, "guid": _guid(rng)})
    if store and store.has("MeIdentifier") and owner_rowid is not None:
        db.conn.execute('UPDATE ABStore SET MeIdentifier = ? WHERE ROWID = ?', (owner_rowid, store_id))
    return rowids
