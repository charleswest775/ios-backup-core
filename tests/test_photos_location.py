"""Photo locations: iOS writes -180 to ZLATITUDE and ZLONGITUDE when a photo has no location.

Found on the public Hickman backups: on iOS 17.3, 392 of 567 assets hold -180 / -180 and none are
NULL; on iOS 15.3.1, 212 of 231.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.photos import PhotoExtractor


class _FakeBackup:
    encrypted = False

    def __init__(self, photos_db: str):
        self.photos_db = photos_db

    def get_file(self, relative_path, domain="HomeDomain"):
        if (domain, relative_path) == ("CameraRollDomain", "Media/PhotoData/Photos.sqlite"):
            return self.photos_db
        return None

    def list_files(self, domain=None, path_like=None):
        return []


def _photos(tmp_path, *coordinates):
    path = str(tmp_path / "Photos.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ZASSET (
            Z_PK INTEGER PRIMARY KEY, ZUUID VARCHAR, ZFILENAME VARCHAR, ZDIRECTORY VARCHAR, ZKIND INTEGER,
            ZDATECREATED TIMESTAMP, ZLATITUDE FLOAT, ZLONGITUDE FLOAT, ZTRASHEDSTATE INTEGER
        );
        CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR, ZKIND INTEGER);
        """
    )
    for pk, (lat, lon) in enumerate(coordinates, start=1):
        conn.execute(
            "INSERT INTO ZASSET VALUES (?, ?, ?, 'DCIM/100APPLE', 0, ?, ?, ?, 0)",
            (pk, f"A-{pk}", f"IMG_{pk:04}.HEIC", 705_373_000.0 + pk, lat, lon),
        )
    conn.commit()
    conn.close()
    return _FakeBackup(path)


def _locations(backup):
    photos = PhotoExtractor().list_photos(backup)["photos"]
    return {p["uuid"]: (p["latitude"], p["longitude"]) for p in photos}


def test_minus_180_means_no_location(tmp_path):
    backup = _photos(tmp_path, (-180.0, -180.0), (35.6, -78.8))
    assert _locations(backup) == {"A-1": (None, None), "A-2": (35.6, -78.8)}


def test_minus_180_in_either_coordinate_means_no_location(tmp_path):
    backup = _photos(tmp_path, (-180.0, -78.8), (35.6, -180.0))
    assert _locations(backup) == {"A-1": (None, None), "A-2": (None, None)}


def test_null_coordinates_mean_no_location(tmp_path):
    backup = _photos(tmp_path, (None, None), (35.6, None))
    assert _locations(backup) == {"A-1": (None, None), "A-2": (None, None)}


def test_coordinates_near_minus_180_are_kept(tmp_path):
    backup = _photos(tmp_path, (-16.85, -179.97))  # Taveuni, Fiji: just east of the antimeridian
    assert _locations(backup) == {"A-1": (-16.85, -179.97)}
