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
"""

from __future__ import annotations

import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from backupgen import media
from backupgen.persona import Persona, Snapshot
from backupgen.sqlitegen import SchemaDatabase
from backupgen.writers import CoreData, cocoa_seconds

NO_LOCATION = -180.0
_USER_ALBUM, _ROOT_FOLDER = 2, 3999


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


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(bytes=rng.randbytes(16))).upper()


def populate(db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random,
             renderer: media.Renderer) -> list[WrittenPhoto]:
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
    written = []
    for number, (when, photo) in enumerate(taken, start=1):
        filename = f"IMG_{number:04d}.JPG"
        local = when.astimezone(persona.tz)
        data, width, height = renderer.jpeg(photo.id)
        located = photo.lat is not None
        stamp = cocoa_seconds(when)
        pk = assets.insert({
            "Z_ENT": core.ent(asset_entity), "Z_OPT": 1,
            "ZUUID": _uuid(rng), "ZDIRECTORY": "DCIM/100APPLE", "ZFILENAME": filename,
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
                                    photo.favorite, photo.trashed, photo.lat, photo.lon, in_albums))
    for title, album in album_ids.items():
        db.conn.execute("UPDATE ZGENERICALBUM SET ZCACHEDCOUNT = ?, ZCACHEDPHOTOSCOUNT = ?, ZCACHEDVIDEOSCOUNT = 0 WHERE Z_PK = ?",
                        (members[title], members[title], album))
    core.finish()
    return written
