"""
Phone number normalization and contact resolution.

Merged from contacts.py (ContactResolver class) and calls.py
(_clean_phone_number, _resolve_contact). Logging removed; all timing
instrumentation stripped — library should be silent.
"""

import re
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ios_backup_core.backup import BackupAccessor


def normalize_phone(phone: str) -> str:
    """Normalize a phone number for matching (strip everything but digits).

    For US numbers, compares last 10 digits.
    Source: ContactResolver.normalize_phone() in contacts.py.
    """
    digits = re.sub(r"[^\d]", "", phone)
    if len(digits) >= 10:
        return digits[-10:]
    return digits


# Most national numbering plans outside North America have 9-digit subscriber
# numbers (FR, ES, PT, BE, ...), so the "last 10 digits" rule above can't match
# "+33 6 12 34 56 78" against "06 12 34 56 78". As a last resort we compare the
# last 9 digits. Keys are prefixed with "~" so they never collide with real
# identifiers; a suffix shared by two different contacts is stored as "" and
# never matched.
_SUFFIX_DIGITS = 9


def phone_suffix_key(phone: str) -> str:
    """Return the last-9-digits lookup key for *phone*, or "" if too short."""
    digits = re.sub(r"[^\d]", "", phone or "")
    if len(digits) < _SUFFIX_DIGITS:
        return ""
    return "~" + digits[-_SUFFIX_DIGITS:]


def add_phone_suffix_key(contacts: dict, phone: str, name: str) -> None:
    """Index *phone* by suffix in *contacts*, blanking keys that are ambiguous."""
    key = phone_suffix_key(phone)
    if not key:
        return
    existing = contacts.get(key)
    if existing is None:
        contacts[key] = name
    elif existing != name:
        contacts[key] = ""


def clean_phone_number(address: str) -> str:
    """Strip non-numeric characters for better matching, except '+'.

    Source: CallExtractor._clean_phone_number() in calls.py.
    """
    if not address:
        return ""
    return ''.join(c for c in address if c.isdigit() or c == '+')


def resolve_contact(identifier: str, contacts: dict) -> str:
    """Look up a display name for a phone/email identifier.

    Merged from contacts.py:resolve_contact() and calls.py:_resolve_contact().
    Handles: exact match, normalized match, +1 prefix toggling, email
    case-insensitive match. Returns empty string (not 'Unknown') when not found
    so callers can apply their own fallback.
    """
    if not identifier:
        return ""

    # Exact match
    if identifier in contacts:
        return contacts[identifier]

    clean = clean_phone_number(identifier)

    if not clean:
        # Maybe it's an email (FaceTime)
        if "@" in identifier and identifier.lower() in contacts:
            return contacts[identifier.lower()]
        # Normalized phone fallback
        norm = normalize_phone(identifier)
        if norm and norm in contacts:
            return contacts[norm]
        return ""

    # Clean match (keep + prefix)
    if clean in contacts:
        return contacts[clean]

    # Normalized (digits-only, last 10)
    norm = normalize_phone(identifier)
    if norm and norm in contacts:
        return contacts[norm]

    # US country code variants
    if len(clean) == 10 and f"+1{clean}" in contacts:
        return contacts[f"+1{clean}"]
    if clean.startswith("+1") and clean[2:] in contacts:
        return contacts[clean[2:]]

    # 10-digit normalized match against +1 prefixed keys
    if norm and len(norm) == 10 and f"+1{norm}" in contacts:
        return contacts[f"+1{norm}"]

    # International vs. national format (e.g. +33 6… vs 06…): last 9 digits
    return contacts.get(phone_suffix_key(identifier)) or ""


class ContactResolver:
    """Resolves phone numbers and emails to contact names from AddressBook.sqlitedb.

    Source: contacts.py — updated to accept BackupAccessor protocol instead of
    the concrete OpenBackup class; logging removed.
    """

    ADDRESS_BOOK_PATH = "Library/AddressBook/AddressBook.sqlitedb"

    def __init__(self):
        self._cache: dict[str, dict] = {}  # keyed by backup udid

    def clear_cache(self, udid: str | None = None):
        """Clear cached contacts for a specific backup (or all)."""
        if udid:
            self._cache.pop(udid, None)
        else:
            self._cache.clear()

    @staticmethod
    def normalize_phone(phone: str) -> str:
        """Normalize a phone number for matching (strip everything but digits)."""
        digits = re.sub(r"[^\d]", "", phone)
        if len(digits) >= 10:
            return digits[-10:]
        return digits

    def load_contacts(self, backup) -> dict:
        """Load contacts from the backup and return a lookup dict.

        Keys are phone numbers/emails, values are display names.
        Accepts any object with .udid and .get_file() — BackupAccessor protocol.
        """
        udid = backup.udid
        if udid in self._cache:
            return self._cache[udid]

        contacts: dict = {}
        db_path = backup.get_file(self.ADDRESS_BOOK_PATH, domain="HomeDomain")
        if not db_path:
            self._cache[udid] = contacts
            return contacts

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = TRUE")
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute("PRAGMA cache_size = -10000")
            conn.execute("PRAGMA temp_store = MEMORY")

            # Get all person records
            persons: dict[int, str] = {}
            for row in conn.execute("""
                SELECT ROWID, First, Last, Organization
                FROM ABPerson
            """).fetchall():
                first = row["First"] or ""
                last = row["Last"] or ""
                org = row["Organization"] or ""
                name = f"{first} {last}".strip() or org
                if name:
                    persons[row["ROWID"]] = name

            # Map phone numbers to person names
            for row in conn.execute("""
                SELECT record_id, value
                FROM ABMultiValue
                WHERE property = 3
            """).fetchall():  # property 3 = phone numbers
                person_id = row["record_id"]
                if person_id in persons:
                    phone = row["value"]
                    contacts[phone] = persons[person_id]
                    normalized = self.normalize_phone(phone)
                    if normalized:
                        contacts[normalized] = persons[person_id]
                        contacts[f"+1{normalized}"] = persons[person_id]
                    add_phone_suffix_key(contacts, phone, persons[person_id])

            # Map email addresses to person names
            for row in conn.execute("""
                SELECT record_id, value
                FROM ABMultiValue
                WHERE property = 4
            """).fetchall():  # property 4 = email addresses
                person_id = row["record_id"]
                if person_id in persons:
                    email = row["value"]
                    contacts[email] = persons[person_id]
                    contacts[email.lower()] = persons[person_id]

            conn.close()
        except Exception:
            pass

        self._cache[udid] = contacts
        return contacts

    def list_contacts(self, backup) -> dict:
        """Get full contact list with all details."""
        db_path = backup.get_file(self.ADDRESS_BOOK_PATH, domain="HomeDomain")
        if not db_path:
            return {"contacts": []}

        contact_list = []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

            persons = conn.execute("""
                SELECT ROWID, First, Last, Organization, Department, Note
                FROM ABPerson
                ORDER BY First, Last
            """).fetchall()

            for person in persons:
                pid = person["ROWID"]
                first = person["First"] or ""
                last = person["Last"] or ""

                phones = [
                    row["value"]
                    for row in conn.execute(
                        "SELECT value FROM ABMultiValue WHERE record_id = ? AND property = 3",
                        (pid,)
                    ).fetchall()
                ]

                emails = [
                    row["value"]
                    for row in conn.execute(
                        "SELECT value FROM ABMultiValue WHERE record_id = ? AND property = 4",
                        (pid,)
                    ).fetchall()
                ]

                contact_list.append({
                    "id": pid,
                    "first_name": first,
                    "last_name": last,
                    "display_name": f"{first} {last}".strip() or person["Organization"] or "Unknown",
                    "organization": person["Organization"],
                    "phones": phones,
                    "emails": emails,
                    "note": person["Note"],
                })

            conn.close()
        except Exception:
            pass

        return {"contacts": contact_list}
