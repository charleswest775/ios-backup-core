"""
Photo/video metadata extraction from CameraRollDomain.

Extracted from openextract/python/photos.py:PhotoExtractor.
Changes:
  - _APPLE_EPOCH replaced with import from ios_backup_core.timestamps
  - Thumbnail generation and base64 encoding removed — UI concern
  - PIL/pillow-heif imports removed
  - export_* methods removed
  - list_photos() returns metadata only (no binary data)
"""

import os
import re
import sqlite3
import sys
import uuid
from typing import Optional

from ios_backup_core.db import connect_readonly, get_database
from ios_backup_core.timestamps import APPLE_EPOCH

# ZASSET.ZKIND → kind label
_KIND_MAP = {
    0: "photo",
    1: "video",
    2: "live_photo",
    3: "live_photo",
}

# ZGENERICALBUM.ZKIND values. 2, 1505, 3571–3573 and 3998/3999 hold from iOS 13 to 17
# in the public Hickman backups; none of those phones has a folder, so 4000 comes from
# the Photos.sqlite documentation.
_ALBUM_KIND_USER = 2
_ALBUM_KIND_SMART = 3
_ALBUM_KIND_SHARED = 1505
_ALBUM_KIND_FOLDER = 4000
# Photos' own bookkeeping, never shown on the phone: "progress-sync",
# "progress-ota-restore" and "progress-fs-import" (3571–3573), and the root folders
# of the library and of projects (3999, 3998).
_INTERNAL_ALBUM_KINDS = {3571, 3572, 3573, 3998, 3999}

# Album IDs list_albums offers besides the backup's own albums, and list_photos accepts.
ALL_PHOTOS = "__all__"
FAVORITES = "__favorites__"
HIDDEN = "__hidden__"
# list_photos also accepts this one, which list_albums doesn't offer: every photo outside
# Recently Deleted, hidden ones too, for saving a complete copy of the library.
LIBRARY = "__library__"

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".tiff", ".bmp"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi"}

_MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif",
    ".heic": "image/heic", ".heif": "image/heif",
    ".mov": "video/quicktime", ".mp4": "video/mp4",
    ".m4v": "video/mp4",
}


def _apple_ts_to_iso(ts) -> Optional[str]:
    """Convert Apple CoreData timestamp (seconds since 2001-01-01) to ISO 8601."""
    if ts is None:
        return None
    try:
        import datetime
        dt = APPLE_EPOCH + datetime.timedelta(seconds=float(ts))
        return dt.isoformat()
    except Exception:
        return None


def asset_table(conn: sqlite3.Connection) -> str:
    """The assets table: ZASSET from iOS 14, ZGENERICASSET before that."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return "ZGENERICASSET" if "ZASSET" not in tables and "ZGENERICASSET" in tables else "ZASSET"


def _column(row: sqlite3.Row, *names):
    """The first of ``names`` that exists in ``row``, or None.

    ZASSET columns are renamed between releases (iOS 15 has ZMODIFICATIONDATE and
    ZAVALANCHEUUID, not ZDATEMODIFIED and ZBURSTUUID), and indexing a missing
    column raises, which used to fail the whole listing.
    """
    keys = row.keys()
    for name in names:
        if name in keys:
            return row[name]
    return None


# iOS writes -180 to both ZLATITUDE and ZLONGITUDE when an asset has no
# location; it doesn't leave them NULL.
_NO_LOCATION = -180.0


def _location(latitude, longitude) -> tuple:
    """(latitude, longitude) as floats, or (None, None) when the asset has no location."""
    if latitude is None or longitude is None:
        return None, None
    latitude, longitude = float(latitude), float(longitude)
    if _NO_LOCATION in (latitude, longitude):
        return None, None
    return latitude, longitude


def uuid_text(value) -> Optional[str]:
    """A UUID column as the uppercase string iOS 7 and later store, whatever its storage class.

    iOS 6's Photos.sqlite declares ZGENERICASSET.ZUUID as a BLOB (iOS 7 made it
    VARCHAR), and sqlite3 hands a BLOB over as bytes, which a JSON result can't
    hold: OpenExtract's engine exited on it (in-app reports OE-26 to OE-28).
    16 bytes are the UUID itself. Other bytes are taken as UTF-8 text when they
    are that, else given as hex, so the result never carries raw bytes.
    """
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return value
    raw = bytes(value)
    if len(raw) == 16:
        return str(uuid.UUID(bytes=raw)).upper()
    try:
        return raw.decode("utf-8").rstrip("\x00")
    except UnicodeDecodeError:
        return raw.hex().upper()


def _build_dcim_path(directory: str, filename: str) -> str:
    """Build the CameraRollDomain-relative path for a DCIM asset.

    ZDIRECTORY varies by iOS version:
      Modern iOS:  "106APPLE"       → Media/DCIM/106APPLE/IMG.HEIC
      Older iOS:   "DCIM/106APPLE"  → same (strip leading DCIM/)
    """
    directory = directory.lstrip("/")
    if directory.upper() == "DCIM":
        directory = ""
    elif directory.upper().startswith("DCIM/"):
        directory = directory[5:]
    if directory:
        return f"Media/DCIM/{directory}/{filename}"
    return f"Media/DCIM/{filename}"


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    """The column names of ``table``; empty when there's no such table."""
    return {col[1] for col in conn.execute(f"PRAGMA table_info('{table}')")}


def smart_album_where(columns: set, album_id: Optional[str]) -> Optional[str]:
    """The asset-table condition for All Photos, Favorites, Hidden or LIBRARY; None for the backup's own albums.

    ``columns`` are the asset table's columns. None of them lists Recently Deleted
    (ZTRASHEDSTATE, iOS 8 and later). All Photos and Favorites leave out hidden
    photos, as the Photos app does; they're under Hidden, and LIBRARY lists them with
    everything else. A library with no ZFAVORITE or ZHIDDEN column lists nothing for
    that one.
    """
    live = "ZTRASHEDSTATE = 0" if "ZTRASHEDSTATE" in columns else "1 = 1"
    not_hidden = " AND COALESCE(ZHIDDEN, 0) = 0" if "ZHIDDEN" in columns else ""
    if album_id == LIBRARY:
        return live
    if not album_id or album_id == ALL_PHOTOS:
        return f"{live}{not_hidden}"
    if album_id == FAVORITES:
        if "ZFAVORITE" not in columns:
            return "0 = 1"
        return f"{live} AND ZFAVORITE = 1{not_hidden}"
    if album_id == HIDDEN:
        return f"{live} AND ZHIDDEN = 1" if "ZHIDDEN" in columns else "0 = 1"
    return None


def _album_rows(conn: sqlite3.Connection) -> Optional[list]:
    """The titled rows of ZGENERICALBUM the Photos app shows, in the phone's order; None without the table.

    Each row has Z_PK, ZTITLE, ZKIND, PARENT (ZPARENTFOLDER) and POSITION, the
    album's place in its folder (Z_FOK_PARENTFOLDER, the ordering column Core Data
    keeps for the folder's albums). Albums with a place come in that order, then the
    rest by title, then shared albums (which sit in no folder) by title. Leaves out
    Photos' internal albums and albums in Recently Deleted.
    """
    columns = _table_columns(conn, "ZGENERICALBUM")
    if not {"Z_PK", "ZTITLE"} <= columns:
        return None

    def column(name: str) -> str:
        return name if name in columns else "NULL"

    where = "ZTITLE IS NOT NULL"
    if "ZTRASHEDSTATE" in columns:
        where += " AND COALESCE(ZTRASHEDSTATE, 0) = 0"
    rows = conn.execute(
        f"SELECT Z_PK, ZTITLE, {column('ZKIND')} AS ZKIND, {column('ZPARENTFOLDER')} AS PARENT, "
        f"{column('Z_FOK_PARENTFOLDER')} AS POSITION FROM ZGENERICALBUM WHERE {where}"
    ).fetchall()
    rows = [row for row in rows if row["ZKIND"] not in _INTERNAL_ALBUM_KINDS]
    rows.sort(key=lambda row: (row["ZKIND"] == _ALBUM_KIND_SHARED, row["POSITION"] is None,
                               row["POSITION"] or 0, str(row["ZTITLE"]).casefold()))
    return rows


def _album_parents(listed: dict) -> dict:
    """{album Z_PK: Z_PK of the listed folder it's in}, for the albums that are in one.

    ``listed`` maps Z_PK to the rows _album_rows returned. An album whose parent isn't
    listed (the library's root folder, a folder in Recently Deleted) is at the top
    level. A loop of parents, which only a damaged library could hold, is cut where
    it closes, so every album still shows.
    """
    parents = {pk: row["PARENT"] for pk, row in listed.items()
               if row["PARENT"] in listed and row["PARENT"] != pk}
    for start in list(parents):
        node, seen = start, {start}
        while node in parents:
            if parents[node] in seen:
                del parents[node]
                break
            node = parents[node]
            seen.add(node)
    return parents


def _depth_first(rows: list, parents: dict) -> list:
    """``rows`` with each folder followed by what's in it, keeping their order within a folder."""
    children: dict = {}
    for row in rows:
        children.setdefault(parents.get(row["Z_PK"]), []).append(row)
    ordered: list = []
    stack = list(reversed(children.get(None, [])))
    while stack:
        row = stack.pop()
        ordered.append(row)
        stack.extend(reversed(children.get(row["Z_PK"], [])))
    return ordered


class PhotoExtractor:
    """Extracts photo and video metadata from iOS backups."""

    def _open_photos_db(self, backup) -> Optional[sqlite3.Connection]:
        """Open Photos.sqlite from the backup. Returns a connection or None."""
        db_path = get_database(backup, "Media/PhotoData/Photos.sqlite", domain="CameraRollDomain")
        if not db_path or not os.path.exists(db_path):
            db_path = get_database(backup, "PhotoData/Photos.sqlite", domain="CameraRollDomain")
        if not db_path or not os.path.exists(db_path):
            return None
        try:
            conn = connect_readonly(db_path, backup)
        except sqlite3.Error:
            return None
        conn.row_factory = sqlite3.Row
        return conn

    def _find_album_junction(self, conn: sqlite3.Connection):
        """Dynamically locate the junction table between ZGENERICALBUM and ZASSET.

        Returns (table_name, albums_col, assets_col) or (None, None, None).
        """
        try:
            pk_rows = conn.execute(
                "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'GenericAlbum'"
            ).fetchall()
            for (ent_num,) in pk_rows:
                table = f"Z_{ent_num}ASSETS"
                try:
                    pragma = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
                    col_names = [col[1] for col in pragma]
                    albums_col = next((c for c in col_names if c.endswith("ALBUMS")), None)
                    assets_col = next((c for c in col_names if c.endswith("ASSETS")), None)
                    if albums_col and assets_col:
                        return table, albums_col, assets_col
                except Exception:
                    pass
        except Exception:
            pass

        try:
            all_tables = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            for table in all_tables:
                if not re.match(r"^Z_\d+ASSETS$", table):
                    continue
                pragma = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
                col_names = [col[1] for col in pragma]
                albums_col = next((c for c in col_names if c.endswith("ALBUMS")), None)
                assets_col = next((c for c in col_names if c.endswith("ASSETS")), None)
                if albums_col and assets_col:
                    return table, albums_col, assets_col
        except Exception as e:
            print(f"[photos] _find_album_junction error: {e}", file=sys.stderr, flush=True)

        return None, None, None

    def _asset_row_to_dict(self, row, album_ids: list, file_hash: str) -> dict:
        """Convert a ZASSET sqlite3.Row to a photo asset dict."""
        filename = row["ZFILENAME"] or ""
        kind_int = row["ZKIND"] if row["ZKIND"] is not None else 0
        kind = _KIND_MAP.get(kind_int, "unknown")
        width = _column(row, "ZWIDTH")
        height = _column(row, "ZHEIGHT")
        duration = _column(row, "ZDURATION")
        latitude, longitude = _location(_column(row, "ZLATITUDE"), _column(row, "ZLONGITUDE"))

        return {
            "uuid": uuid_text(_column(row, "ZUUID")) or "",
            "filename": filename,
            "file_hash": file_hash,
            "kind": kind,
            "date_created": _apple_ts_to_iso(_column(row, "ZDATECREATED")),
            "date_modified": _apple_ts_to_iso(_column(row, "ZMODIFICATIONDATE", "ZDATEMODIFIED")),
            "width": width if width is not None else 0,
            "height": height if height is not None else 0,
            "duration": float(duration) if duration is not None else 0.0,
            "favorite": bool(_column(row, "ZFAVORITE")),
            "hidden": bool(_column(row, "ZHIDDEN")),
            "has_adjustments": bool(_column(row, "ZHASADJUSTMENTS")),
            "burst_uuid": uuid_text(_column(row, "ZAVALANCHEUUID", "ZBURSTUUID")),
            "latitude": latitude,
            "longitude": longitude,
            "album_ids": album_ids,
        }

    def list_albums(self, backup) -> dict:
        """List the photo albums in Photos.sqlite, as the Photos app shows them.

        The list starts with All Photos, then Favorites and Hidden when the library
        records them, then the backup's own albums and folders. Each entry has ``id``,
        ``title``, ``asset_count``, ``kind`` ("smart", "user", "shared" or "folder")
        and ``parent_id``: the id of the folder it's in, or None at the top level.
        Albums keep the phone's order within their folder, with shared albums last.
        Photos' internal albums and albums in Recently Deleted are left out.
        """
        conn = self._open_photos_db(backup)
        if not conn:
            return {"albums": [], "source": "unavailable"}

        try:
            junc_table, albums_col, assets_col = self._find_album_junction(conn)

            assets = asset_table(conn)
            columns = _table_columns(conn, assets)

            def count(album_id: str) -> int:
                where = smart_album_where(columns, album_id)
                return conn.execute(f"SELECT COUNT(*) FROM {assets} WHERE {where}").fetchone()[0]

            album_counts: dict = {}
            if junc_table and albums_col and assets_col:
                try:
                    count_rows = conn.execute(
                        f"SELECT {albums_col}, COUNT(*) FROM '{junc_table}' "
                        f"WHERE {albums_col} IS NOT NULL GROUP BY {albums_col}"
                    ).fetchall()
                    album_counts = {r[0]: r[1] for r in count_rows}
                except Exception:
                    pass

            rows = _album_rows(conn)
            if rows is None:
                conn.close()
                return {"albums": [], "source": "unavailable"}
            listed = {row["Z_PK"]: row for row in rows}
            parents = _album_parents(listed)
            folders = set(parents.values())
            rows = _depth_first(rows, parents)

            def album_kind(row) -> str:
                if row["ZKIND"] == _ALBUM_KIND_FOLDER or row["Z_PK"] in folders:
                    return "folder"
                if row["ZKIND"] == _ALBUM_KIND_SMART:
                    return "smart"
                if row["ZKIND"] == _ALBUM_KIND_SHARED:
                    return "shared"
                return "user"

            albums = [{"id": ALL_PHOTOS, "title": "All Photos", "asset_count": count(ALL_PHOTOS),
                       "kind": "smart", "parent_id": None}]
            if "ZFAVORITE" in columns:
                albums.append({"id": FAVORITES, "title": "Favorites", "asset_count": count(FAVORITES),
                               "kind": "smart", "parent_id": None})
            if "ZHIDDEN" in columns:
                albums.append({"id": HIDDEN, "title": "Hidden", "asset_count": count(HIDDEN),
                               "kind": "smart", "parent_id": None})
            for row in rows:
                parent = parents.get(row["Z_PK"])
                albums.append({
                    "id": str(row["Z_PK"]),
                    "title": row["ZTITLE"],
                    "asset_count": album_counts.get(row["Z_PK"], 0),
                    "kind": album_kind(row),
                    "parent_id": str(parent) if parent is not None else None,
                })

            conn.close()
            return {"albums": albums, "source": "photos_sqlite"}

        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return {"albums": [], "error": str(e), "source": "error"}

    def list_photos(
        self,
        backup,
        offset: int = 0,
        limit: int = 100,
        album_id: Optional[str] = None,
    ) -> dict:
        """List photo assets with rich metadata (no binary data).

        ``album_id`` is an id from list_albums, or LIBRARY for every photo with the
        hidden ones; None lists All Photos, which leaves hidden photos out.
        Returns metadata dicts; callers use backup.get_file() to read pixels.
        """
        conn = self._open_photos_db(backup)
        if not conn:
            return {"photos": [], "total": 0, "source": "unavailable"}

        try:
            junc_table, albums_col, assets_col = self._find_album_junction(conn)

            # Build asset → album_ids mapping
            asset_albums: dict[int, list[str]] = {}
            if junc_table and albums_col and assets_col:
                try:
                    for jr in conn.execute(
                        f"SELECT {albums_col}, {assets_col} FROM '{junc_table}' "
                        f"WHERE {albums_col} IS NOT NULL AND {assets_col} IS NOT NULL"
                    ).fetchall():
                        asset_albums.setdefault(jr[1], []).append(str(jr[0]))
                except Exception:
                    pass

            # Hash lookup table: ZASSET.Z_PK → fileID from Manifest
            # (omitted — callers use backup.get_file with the ZDIRECTORY/ZFILENAME path)

            assets = asset_table(conn)
            columns = _table_columns(conn, assets)
            params: list = []

            where = smart_album_where(columns, album_id)
            if where is None and junc_table and albums_col and assets_col:
                base_query = (
                    f"SELECT a.* FROM {assets} a "
                    f"JOIN '{junc_table}' j ON j.{assets_col} = a.Z_PK "
                    f"WHERE j.{albums_col} = ?"
                )
                params.append(int(album_id))
            else:
                # The whole library when the backup's albums can't be read.
                base_query = f"SELECT * FROM {assets} WHERE {where or smart_album_where(columns, ALL_PHOTOS)}"

            base_query += " ORDER BY ZDATECREATED DESC"

            # Total count
            count_sql = f"SELECT COUNT(*) FROM ({base_query})"
            total = conn.execute(count_sql, params).fetchone()[0]

            # Paged fetch
            paged_sql = base_query + f" LIMIT {limit} OFFSET {offset}"
            rows = conn.execute(paged_sql, params).fetchall()

            photos = []
            for row in rows:
                pk = row["Z_PK"]
                album_ids = asset_albums.get(pk, [])
                asset = self._asset_row_to_dict(row, album_ids, "")

                # Compute the CameraRollDomain relative path for this asset
                directory = row["ZDIRECTORY"] or ""
                filename = row["ZFILENAME"] or ""
                if directory and filename:
                    asset["relative_path"] = _build_dcim_path(directory, filename)
                else:
                    asset["relative_path"] = None

                photos.append(asset)

            conn.close()
            return {
                "photos": photos,
                "total": total,
                "offset": offset,
                "limit": limit,
                "source": "photos_sqlite",
            }

        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return {"photos": [], "total": 0, "error": str(e), "source": "error"}

    def get_file_path(self, backup, asset: dict) -> Optional[str]:
        """Return the local path for an asset returned by list_photos().

        Uses the 'relative_path' field set by list_photos().
        """
        rel = asset.get("relative_path")
        if not rel:
            return None
        return backup.get_file(rel, domain="CameraRollDomain")
