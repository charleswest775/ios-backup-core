"""Photos: Media/PhotoData/Photos.sqlite (a Core Data store) and the originals in Media/DCIM.

Rows follow what iOS 13–26 write:
- One asset row per photo in ZASSET (ZGENERICASSET before iOS 14), with ZDIRECTORY
  "DCIM/100APPLE" and ZFILENAME "IMG_0001.JPG" pointing at CameraRollDomain
  Media/DCIM/100APPLE/IMG_0001.JPG. Dates are seconds since 2001-01-01.
- ZLATITUDE/ZLONGITUDE hold the location, or -180 when there isn't one.
- ZADDITIONALASSETATTRIBUTES keeps the original file name, size and time zone;
  ZEXTENDEDATTRIBUTES the camera and location from the EXIF.
- Albums are ZGENERICALBUM rows (ZKIND 2) under the library's root folder (ZKIND 3999),
  linked to assets through the Z_<album entity>ASSETS join with its ordering column.
- Z_RT_Asset_boundedByRect, the R-tree location index, gets a row per located photo.
- A photo in Recently Deleted has ZTRASHEDSTATE 1 and its trash date.
- The People album: a ZPERSON row per group of faces (ZFULLNAME when named, ZPERSONURI
  the linked card's ABPerson.guid, ZMERGETARGETPERSON for a group merged into another,
  ZDETECTIONTYPE 1 for a person, 3 a dog, 4 a cat) and a ZDETECTEDFACE row per face, its
  box in ZCENTERX/ZCENTERY/ZSIZE (fractions of the photo). Its links are ZPERSON/ZASSET
  before iOS 17 and ZPERSONFORFACE/ZASSETFORFACE from it. A real phone's ZFACECOUNT is
  often stale; here it's right.
"""

from __future__ import annotations

import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from backupgen import media
from backupgen.persona.core import Persona, Snapshot
from backupgen.sqlitegen import SchemaDatabase
from backupgen.writers import CoreData, cocoa_seconds
from backupgen.writers.context import Step, StepContext

NO_LOCATION = -180.0
_USER_ALBUM, _ROOT_FOLDER = 2, 3999
_DETECTION = {None: 1, "dog": 3, "cat": 4}  # ZDETECTIONTYPE


@dataclass
class WrittenPhoto:
    """One photo as written, for the answer key and the DCIM file."""

    id: str
    filename: str
    relative_path: str
    data: bytes
    date: datetime
    width: int
    height: int
    favorite: bool
    trashed: bool
    lat: Optional[float]
    lon: Optional[float]
    albums: list[str]
    uuid: str = ""


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(bytes=rng.randbytes(16))).upper()


def populate(db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random,
             renderer: media.Renderer, contact_guids: Optional[dict[str, str]] = None) -> list[WrittenPhoto]:
    """Write the library; ``contact_guids`` (contact id → ABPerson.guid) links People groups to cards."""
    core = CoreData(db)
    asset_table = "ZASSET" if db.table("ZASSET") else "ZGENERICASSET"
    assets = db.table(asset_table)
    asset_entity = "Asset"
    additional, extended, albums_table = db.table("ZADDITIONALASSETATTRIBUTES"), db.table("ZEXTENDEDATTRIBUTES"), db.table("ZGENERICALBUM")
    join_name = next(t for t in db.schema["columns"] if re.match(r"^Z_\d+ASSETS$", t))
    join_columns = [c["name"] for c in db.schema["columns"][join_name]]
    album_column = next(c for c in join_columns if c.endswith("ALBUMS"))
    asset_column = next(c for c in join_columns if c.endswith("ASSETS") and not c.startswith("Z_FOK_"))
    order_column = next((c for c in join_columns if c.startswith("Z_FOK_")), None)
    rtree = next((o["name"] for o in db.schema["objects"] if o["type"] == "virtual" and "boundedByRect" in o["name"]), None)

    taken = []
    for photo in persona.photos or []:
        when = persona.device_time(photo.real_time) + timedelta(seconds=rng.randrange(60))
        if when <= snapshot.backup_date:
            taken.append((when, photo))
    taken.sort(key=lambda t: t[0])

    root = albums_table.insert({"Z_ENT": core.ent("Folder"), "Z_OPT": 1, "ZKIND": _ROOT_FOLDER,
                                "ZUUID": _uuid(rng), "ZTRASHEDSTATE": 0})
    core.saw("GenericAlbum", root)
    album_ids: dict[str, int] = {}
    members: dict[str, int] = {}
    asset_pks: dict[str, int] = {}
    written = []
    for number, (when, photo) in enumerate(taken, start=1):
        filename = f"IMG_{number:04d}.JPG"
        local = when.astimezone(persona.tz)
        data, width, height = renderer.jpeg(photo.id)
        located = photo.lat is not None
        stamp = cocoa_seconds(when)
        asset_uuid = _uuid(rng)
        pk = assets.insert({
            "Z_ENT": core.ent(asset_entity), "Z_OPT": 1,
            "ZUUID": asset_uuid, "ZDIRECTORY": "DCIM/100APPLE", "ZFILENAME": filename,
            "ZKIND": 0, "ZKINDSUBTYPE": 0, "ZUNIFORMTYPEIDENTIFIER": "public.jpeg", "ZSAVEDASSETTYPE": 3,
            "ZDATECREATED": stamp, "ZADDEDDATE": stamp + 2, "ZMODIFICATIONDATE": stamp + 2,
            "ZWIDTH": width, "ZHEIGHT": height, "ZORIENTATION": 1, "ZDURATION": 0.0,
            "ZLATITUDE": photo.lat if located else NO_LOCATION, "ZLONGITUDE": photo.lon if located else NO_LOCATION,
            "ZFAVORITE": int(photo.favorite), "ZHIDDEN": 0, "ZHASADJUSTMENTS": 0,
            "ZTRASHEDSTATE": int(photo.trashed), "ZTRASHEDDATE": cocoa_seconds(snapshot.backup_date) - 86400 if photo.trashed else None,
            "ZCOMPLETE": 1, "ZVISIBILITYSTATE": 0, "ZCLOUDLOCALSTATE": 0,
        })
        core.saw(asset_entity, pk)
        offset = local.utcoffset()
        extras = additional.insert({
            "Z_ENT": core.ent("AdditionalAssetAttributes"), "Z_OPT": 1, "ZASSET": pk,
            "ZORIGINALFILENAME": filename, "ZORIGINALFILESIZE": len(data), "ZORIGINALWIDTH": width, "ZORIGINALHEIGHT": height,
            "ZTIMEZONENAME": f"GMT{local.strftime('%z')}", "ZTIMEZONEOFFSET": int(offset.total_seconds()),
            "ZEXIFTIMESTAMPSTRING": local.strftime("%Y:%m:%d %H:%M:%S"),
        })
        core.saw("AdditionalAssetAttributes", extras)
        details = extended.insert({
            "Z_ENT": core.ent("ExtendedAttributes"), "Z_OPT": 1, "ZASSET": pk,
            "ZCAMERAMAKE": "Apple", "ZCAMERAMODEL": snapshot.product_name,
            "ZLATITUDE": photo.lat if located else None, "ZLONGITUDE": photo.lon if located else None,
            "ZDATECREATED": stamp, "ZTIMEZONENAME": f"GMT{local.strftime('%z')}", "ZTIMEZONEOFFSET": int(offset.total_seconds()),
        })
        core.saw("ExtendedAttributes", details)
        db.conn.execute(f'UPDATE "{asset_table}" SET ZADDITIONALATTRIBUTES = ?, ZEXTENDEDATTRIBUTES = ? WHERE Z_PK = ?',
                        (extras, details, pk))
        if located and rtree:
            db.conn.execute(f'INSERT INTO "{rtree}" VALUES (?, ?, ?, ?, ?)', (pk, photo.lat, photo.lat, photo.lon, photo.lon))
        in_albums = [] if photo.trashed else photo.albums
        for title in in_albums:
            if title not in album_ids:
                album_ids[title] = albums_table.insert({
                    "Z_ENT": core.ent("Album"), "Z_OPT": 1, "ZKIND": _USER_ALBUM, "ZTITLE": title, "ZUUID": _uuid(rng),
                    "ZTRASHEDSTATE": 0, "ZPARENTFOLDER": root, "ZCREATIONDATE": stamp,
                })
                core.saw("GenericAlbum", album_ids[title])
            members[title] = members.get(title, 0) + 1
            row = {album_column: album_ids[title], asset_column: pk}
            if order_column:
                row[order_column] = members[title] * 1024
            db.table(join_name).insert(row)
        written.append(WrittenPhoto(photo.id, filename, f"Media/DCIM/100APPLE/{filename}", data, when, width, height,
                                    photo.favorite, photo.trashed, photo.lat, photo.lon, in_albums, asset_uuid))
        asset_pks[photo.id] = pk
    for title, album in album_ids.items():
        db.conn.execute("UPDATE ZGENERICALBUM SET ZCACHEDCOUNT = ?, ZCACHEDPHOTOSCOUNT = ?, ZCACHEDVIDEOSCOUNT = 0 WHERE Z_PK = ?",
                        (members[title], members[title], album))
    if persona.photo_people and db.table("ZPERSON") and db.table("ZDETECTEDFACE"):
        _write_people(db, core, persona, snapshot, [(photo, asset_pks[photo.id]) for _, photo in taken],
                      contact_guids or {})
    core.finish()
    return written


def _write_people(db: SchemaDatabase, core: CoreData, persona: Persona, snapshot: Snapshot,
                  photos: list, contact_guids: dict[str, str]) -> None:
    """The People album: ZPERSON rows, then a ZDETECTEDFACE row per face on ``photos`` ((Photo, asset Z_PK))."""
    # Its own random numbers, so adding people to a pack leaves the rest of its backups as they were.
    rng = random.Random(f"{persona.id}/{snapshot.id}/photo-people")
    people, faces = db.table("ZPERSON"), db.table("ZDETECTEDFACE")
    person_column = "ZPERSONFORFACE" if faces.has("ZPERSONFORFACE") else "ZPERSON"
    asset_column = "ZASSETFORFACE" if faces.has("ZASSETFORFACE") else "ZASSET"
    pks: dict[str, int] = {}
    for person in persona.photo_people:
        contact = persona.contacts.get(person.contact) if person.contact else None
        name = person.name or (contact.display_name if contact else None)
        pks[person.id] = people.insert({
            "Z_ENT": core.ent("Person"), "Z_OPT": 1, "ZPERSONUUID": _uuid(rng),
            "ZFULLNAME": name, "ZDISPLAYNAME": name.split()[0] if name else None,
            "ZPERSONURI": contact_guids.get(person.contact) if person.contact else None,
            "ZDETECTIONTYPE": _DETECTION[person.pet], "ZTYPE": 0, "ZVERIFIEDTYPE": 2 if name else 0,
            "ZCLOUDVERIFIEDTYPE": 2 if name else 0, "ZMANUALORDER": -1, "ZQUESTIONTYPE": 0,
        })
        core.saw("Person", pks[person.id])
    by_id = {person.id: person for person in persona.photo_people}
    counts: dict[str, int] = {}
    key_faces: dict[str, int] = {}
    for photo, asset in photos:
        for face in photo.faces:
            person = by_id[face.person]
            pk = faces.insert({
                "Z_ENT": core.ent("DetectedFace"), "Z_OPT": 1, "ZUUID": _uuid(rng),
                person_column: pks[person.id], asset_column: asset,
                "ZCENTERX": face.x, "ZCENTERY": face.y, "ZSIZE": face.size,
                "ZHIDDEN": int(face.hidden), "ZMANUAL": 0, "ZASSETVISIBLE": int(not photo.trashed),
                "ZDETECTIONTYPE": _DETECTION[person.pet], "ZNAMESOURCE": 0, "ZTRAININGTYPE": 0,
            })
            core.saw("DetectedFace", pk)
            counts[person.id] = counts.get(person.id, 0) + 1
            if person.id not in key_faces and person.key in (None, photo.id):
                key_faces[person.id] = pk
    for person_id, pk in pks.items():
        target = by_id[person_id].merged_into
        db.conn.execute("UPDATE ZPERSON SET ZFACECOUNT = ?, ZKEYFACE = ?, ZMERGETARGETPERSON = ? WHERE Z_PK = ?",
                        (counts.get(person_id, 0), key_faces.get(person_id), pks[target] if target else None, pk))
        if person_id in key_faces and faces.has("ZPERSONBEINGKEYFACE"):
            db.conn.execute("UPDATE ZDETECTEDFACE SET ZPERSONBEINGKEYFACE = ? WHERE Z_PK = ?", (pk, key_faces[person_id]))


def people_answer_key(persona: Persona, written: Optional[list[WrittenPhoto]]) -> list[dict]:
    """The People album as a reader should give it, from the photos as written.

    Every group, with ``photos``: the ids of photos All Photos shows (not in Recently
    Deleted) with one of its faces that isn't hidden, counting the faces of groups merged
    into it; a merged group itself has none.
    """
    by_id = {person.id: person for person in persona.photo_people}

    def final(person_id: str) -> str:
        seen = set()
        while by_id[person_id].merged_into and person_id not in seen:
            seen.add(person_id)
            person_id = by_id[person_id].merged_into
        return person_id

    shown = {p.id for p in written or [] if not p.trashed}
    photos: dict[str, list[str]] = {person.id: [] for person in persona.photo_people}
    for photo in persona.photos or []:
        if photo.id not in shown:
            continue
        for face in photo.faces:
            target = final(face.person)
            if not face.hidden and photo.id not in photos[target]:
                photos[target].append(photo.id)
    out = []
    for person in persona.photo_people:
        contact = persona.contacts.get(person.contact) if person.contact else None
        out.append({
            "id": person.id, "name": person.name or (contact.display_name if contact else None),
            "contact": person.contact, "pet": person.pet, "merged_into": person.merged_into,
            "photos": photos[person.id],
        })
    return out


def step(ctx: StepContext) -> Optional[list[WrittenPhoto]]:
    """Photos.sqlite and the camera roll's files. Returns the photos written."""
    if ctx.persona.photos is None:
        return None
    if ctx.persona.photo_people and not ctx.contact_guids:
        raise RuntimeError("the contacts step runs before photos: the People album links groups to its cards' guids")
    photos: list[WrittenPhoto] = []
    library = ctx.build("Photos.sqlite", lambda db: photos.extend(populate(db, ctx.persona, ctx.snapshot, ctx.rng,
                                                                           ctx.renderer, ctx.contact_guids)))
    ctx.writer.add_file("CameraRollDomain", "Media/PhotoData/Photos.sqlite", library)
    for photo in photos:
        ctx.writer.add_file("CameraRollDomain", photo.relative_path, photo.data)
    return photos


def answer_key(written: Optional[list[WrittenPhoto]]) -> list[dict]:
    return [
        {
            "id": p.id, "filename": p.filename, "date": p.date.isoformat(), "width": p.width, "height": p.height,
            "favorite": p.favorite, "trashed": p.trashed, "lat": p.lat, "lon": p.lon, "albums": p.albums,
            "uuid": p.uuid,
        }
        for p in written or []
    ]


def sections(persona: Persona, written: Mapping[str, Any]) -> dict:
    """The camera roll, then the People album: every group, with the photos a reader should list for it."""
    return {"photos": answer_key(written.get("photos")), "photo_people": people_answer_key(persona, written.get("photos"))}


STEP = Step("photos", step, sections)
