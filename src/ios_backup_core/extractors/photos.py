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
            "uuid": row["ZUUID"] or "",
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
            "burst_uuid": _column(row, "ZAVALANCHEUUID", "ZBURSTUUID"),
            "latitude": latitude,
            "longitude": longitude,
            "album_ids": album_ids,
        }

    def list_albums(self, backup) -> dict:
        """List all photo albums from Photos.sqlite."""
        conn = self._open_photos_db(backup)
        if not conn:
            return {"albums": [], "source": "unavailable"}

        try:
            junc_table, albums_col, assets_col = self._find_album_junction(conn)

            assets = asset_table(conn)
            try:
                total_row = conn.execute(
                    f"SELECT COUNT(*) FROM {assets} WHERE ZTRASHEDSTATE = 0"
                ).fetchone()
                total_assets = total_row[0] if total_row else 0
            except sqlite3.OperationalError:
                total_row = conn.execute(f"SELECT COUNT(*) FROM {assets}").fetchone()
                total_assets = total_row[0] if total_row else 0

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

            try:
                rows = conn.execute(
                    "SELECT Z_PK, ZTITLE, ZKIND FROM ZGENERICALBUM "
                    "WHERE ZTITLE IS NOT NULL ORDER BY ZTITLE ASC"
                ).fetchall()
            except sqlite3.OperationalError:
                conn.close()
                return {"albums": [], "source": "unavailable"}

            def album_kind(k):
                if k in (None, 2):
                    return "user"
                if k == 3:
                    return "smart"
                if k == 1505:
                    return "shared"
                return "user"

            albums = [{
                "id": "__all__",
                "title": "All Photos",
                "asset_count": total_assets,
                "kind": "smart",
            }]
            for row in rows:
                albums.append({
                    "id": str(row["Z_PK"]),
                    "title": row["ZTITLE"],
                    "asset_count": album_counts.get(row["Z_PK"], 0),
                    "kind": album_kind(row["ZKIND"]),
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
            base_query = f"SELECT * FROM {assets}"
            params: list = []

            if album_id and album_id != "__all__" and junc_table and albums_col and assets_col:
                base_query = (
                    f"SELECT a.* FROM {assets} a "
                    f"JOIN '{junc_table}' j ON j.{assets_col} = a.Z_PK "
                    f"WHERE j.{albums_col} = ?"
                )
                params.append(int(album_id))
            else:
                try:
                    base_query += " WHERE ZTRASHEDSTATE = 0"
                except Exception:
                    pass

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
