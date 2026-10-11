"""Database writers. Each fills one harvested schema from a persona snapshot."""

from __future__ import annotations

from typing import Optional

COCOA_EPOCH = 978_307_200  # 2001-01-01 UTC in Unix seconds
_COUNTRY = {"+44": "gb", "+41": "ch", "+1": "us", "+49": "de"}


def cocoa_seconds(moment) -> float:
    return moment.timestamp() - COCOA_EPOCH


def cocoa_nanoseconds(moment) -> int:
    return int(round((moment.timestamp() - COCOA_EPOCH) * 1_000_000_000))


def country_code(number: str) -> Optional[str]:
    """ISO country code iOS stores alongside an E.164 number, for the countries personas use."""
    for prefix, code in _COUNTRY.items():
        if number.startswith(prefix):
            return code
    return None


class CoreData:
    """Core Data bookkeeping for a store: entity numbers from the harvested map, Z_MAX on finish."""

    def __init__(self, db):
        self.db = db
        self.entities = {e["Z_NAME"]: e for e in db.schema["entities"]}
        self.maxima: dict[str, int] = {}

    def ent(self, name: str) -> int:
        return self.entities[name]["Z_ENT"]

    def saw(self, name: str, pk: int) -> None:
        self.maxima[name] = max(self.maxima.get(name, 0), pk)

    def finish(self) -> None:
        table = self.db.table("Z_PRIMARYKEY")
        for entity in self.entities.values():
            table.insert({**entity, "Z_MAX": self.maxima.get(entity["Z_NAME"], 0)})
