"""Photo albums as the Photos app shows them: Favorites, Hidden, folders and the phone's order.

The tables follow the iOS 17 Photos.sqlite of the public Hickman backup: albums are
ZGENERICALBUM rows (ZKIND 2) under the library's root folder (ZKIND 3999) with their place
in it in Z_FOK_PARENTFOLDER, shared albums (ZKIND 1505) sit in no folder, Photos keeps its
own "progress-sync" albums (ZKIND 3571–3573), and Z_28ASSETS joins albums to assets.
A folder is a ZKIND 4000 row that albums point at with ZPARENTFOLDER.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.photos import FAVORITES, HIDDEN, LIBRARY, PhotoExtractor

ROOT, PROJECTS_ROOT, FOLDER, USER, SHARED, PROGRESS = 3999, 3998, 4000, 2, 1505, 3571


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


def _library(tmp_path, albums, assets, members=(), album_columns=True):
    """albums: (pk, kind, title, parent, position, trashed); assets: (pk, favorite, hidden, trashed)."""
    path = str(tmp_path / "Photos.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);
        INSERT INTO Z_PRIMARYKEY VALUES (3, 'Asset', 0, 0), (27, 'GenericAlbum', 0, 0), (28, 'Album', 27, 0);
        CREATE TABLE ZASSET (
            Z_PK INTEGER PRIMARY KEY, ZUUID VARCHAR, ZFILENAME VARCHAR, ZDIRECTORY VARCHAR, ZKIND INTEGER,
            ZDATECREATED TIMESTAMP, ZFAVORITE INTEGER, ZHIDDEN INTEGER, ZTRASHEDSTATE INTEGER
        );
        CREATE TABLE Z_28ASSETS (Z_28ALBUMS INTEGER, Z_3ASSETS INTEGER, Z_FOK_3ASSETS INTEGER);
        """
    )
    if album_columns:
        conn.execute(
            "CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZKIND INTEGER, ZTITLE VARCHAR, "
            "ZPARENTFOLDER INTEGER, Z_FOK_PARENTFOLDER INTEGER, ZTRASHEDSTATE INTEGER)"
        )
        conn.executemany("INSERT INTO ZGENERICALBUM VALUES (?, ?, ?, ?, ?, ?)", albums)
    else:  # an older or reduced library: no folders, order or trash on albums
        conn.execute("CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR, ZKIND INTEGER)")
        conn.executemany("INSERT INTO ZGENERICALBUM VALUES (?, ?, ?)", [(a[0], a[2], a[1]) for a in albums])
    for pk, favorite, hidden, trashed in assets:
        conn.execute(
            "INSERT INTO ZASSET VALUES (?, ?, ?, 'DCIM/100APPLE', 0, ?, ?, ?, ?)",
            (pk, f"A-{pk}", f"IMG_{pk:04}.HEIC", 700_000_000.0 + pk, favorite, hidden, trashed),
        )
    conn.executemany("INSERT INTO Z_28ASSETS VALUES (?, ?, 0)", members)
    conn.commit()
    conn.close()
    return _FakeBackup(path)


def _albums(backup):
    result = PhotoExtractor().list_albums(backup)
    assert result["source"] == "photos_sqlite", result
    return result["albums"]


def _uuids(backup, album_id):
    return [p["uuid"] for p in PhotoExtractor().list_photos(backup, album_id=album_id)["photos"]]


# Asset 1 a favorite, 2 hidden, 3 a hidden favorite, 4 a favorite in Recently Deleted, 5 plain.
ASSETS = [(1, 1, 0, 0), (2, 0, 1, 0), (3, 1, 1, 0), (4, 1, 0, 1), (5, 0, 0, 0)]


def test_favorites_and_hidden_follow_all_photos(tmp_path):
    backup = _library(tmp_path, [(10, ROOT, None, None, None, 0)], ASSETS)
    smart = [(a["id"], a["title"], a["asset_count"], a["kind"]) for a in _albums(backup)[:3]]
    assert smart == [
        ("__all__", "All Photos", 2, "smart"),
        (FAVORITES, "Favorites", 1, "smart"),
        (HIDDEN, "Hidden", 2, "smart"),
    ]
    assert LIBRARY not in [a["id"] for a in _albums(backup)]


def test_favorites_lists_favorites_that_arent_hidden_or_deleted(tmp_path):
    backup = _library(tmp_path, [], ASSETS)
    assert _uuids(backup, FAVORITES) == ["A-1"]


def test_hidden_lists_hidden_photos_that_arent_deleted(tmp_path):
    backup = _library(tmp_path, [], ASSETS)
    assert sorted(_uuids(backup, HIDDEN)) == ["A-2", "A-3"]


def test_all_photos_leaves_out_hidden_photos(tmp_path):
    # As on the iPhone: hidden photos are only under Hidden.
    backup = _library(tmp_path, [], ASSETS)
    assert sorted(_uuids(backup, None)) == ["A-1", "A-5"]
    assert sorted(_uuids(backup, "__all__")) == ["A-1", "A-5"]


def test_library_lists_every_photo_with_the_hidden_ones(tmp_path):
    # For saving a complete copy: everything outside Recently Deleted.
    backup = _library(tmp_path, [], ASSETS)
    assert sorted(_uuids(backup, LIBRARY)) == ["A-1", "A-2", "A-3", "A-5"]
    assert PhotoExtractor().list_photos(backup, album_id=LIBRARY)["total"] == 4


def test_hidden_null_counts_as_not_hidden(tmp_path):
    # Older libraries can leave ZHIDDEN empty; those photos belong in All Photos.
    backup = _library(tmp_path, [], [(1, 0, None, 0), (2, 0, 1, 0)])
    assert _uuids(backup, None) == ["A-1"]
    assert _uuids(backup, HIDDEN) == ["A-2"]


def test_no_favorites_or_hidden_without_their_columns(tmp_path):
    backup = _library(tmp_path, [], [])
    path = backup.photos_db
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        DROP TABLE ZASSET;
        CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID VARCHAR, ZFILENAME VARCHAR, ZDIRECTORY VARCHAR,
                             ZKIND INTEGER, ZDATECREATED TIMESTAMP);
        INSERT INTO ZASSET VALUES (1, 'A-1', 'IMG_0001.JPG', 'DCIM/100APPLE', 0, 1.0);
        """
    )
    conn.close()
    assert [a["id"] for a in _albums(backup)] == ["__all__"]
    assert _uuids(backup, FAVORITES) == [] and _uuids(backup, HIDDEN) == []
    assert _uuids(backup, None) == ["A-1"]


def test_albums_in_a_folder_point_at_it(tmp_path):
    backup = _library(
        tmp_path,
        [
            (10, ROOT, None, None, None, 0),
            (11, FOLDER, "Trips", 10, 1024, 0),
            (12, USER, "Lisbon", 11, 1024, 0),
            (13, USER, "Kyoto", 11, 2048, 0),
            (14, USER, "Screenshots to keep", 10, 2048, 0),
        ],
        ASSETS,
        members=[(12, 1), (12, 5), (13, 2)],
    )
    albums = {a["title"]: a for a in _albums(backup)[3:]}
    assert albums["Trips"]["kind"] == "folder" and albums["Trips"]["parent_id"] is None
    assert albums["Lisbon"]["parent_id"] == albums["Kyoto"]["parent_id"] == "11"
    assert albums["Screenshots to keep"]["parent_id"] is None
    assert albums["Lisbon"]["asset_count"] == 2 and albums["Kyoto"]["asset_count"] == 1
    assert sorted(_uuids(backup, "12")) == ["A-1", "A-5"]


def test_folders_nest(tmp_path):
    backup = _library(
        tmp_path,
        [
            (10, ROOT, None, None, None, 0),
            (11, FOLDER, "Family", 10, 1024, 0),
            (12, FOLDER, "Grandparents", 11, 1024, 0),
            (13, USER, "Visits", 12, 1024, 0),
        ],
        [],
    )
    parents = {a["title"]: a["parent_id"] for a in _albums(backup)[3:]}
    assert parents == {"Family": None, "Grandparents": "11", "Visits": "12"}


def test_each_folder_is_followed_by_what_is_in_it(tmp_path):
    # Places in a folder are only ordered among themselves, so the list mustn't interleave folders.
    backup = _library(
        tmp_path,
        [
            (10, ROOT, None, None, None, 0),
            (11, USER, "Best of", 10, 1024, 0),
            (12, FOLDER, "Trips", 10, 3072, 0),
            (13, FOLDER, "2024", 12, 1024, 0),
            (14, USER, "Rainier", 13, 1024, 0),
            (15, USER, "Hood", 12, 2048, 0),
            (16, USER, "Portland", 10, 2048, 0),
        ],
        [],
    )
    assert [a["title"] for a in _albums(backup)[3:]] == ["Best of", "Portland", "Trips", "2024", "Rainier", "Hood"]


def test_a_row_albums_point_at_is_a_folder_whatever_its_kind(tmp_path):
    # In case a release writes another kind for folders.
    backup = _library(
        tmp_path,
        [(10, ROOT, None, None, None, 0), (11, 4100, "Work", 10, 1024, 0), (12, USER, "Receipts", 11, 1024, 0)],
        [],
    )
    albums = {a["title"]: a for a in _albums(backup)[3:]}
    assert albums["Work"]["kind"] == "folder" and albums["Receipts"]["parent_id"] == "11"


def test_albums_keep_the_phones_order_and_shared_albums_come_last(tmp_path):
    backup = _library(
        tmp_path,
        [
            (10, ROOT, None, None, None, 0),
            (20, SHARED, "Family", None, None, 0),
            (21, USER, "Zoo", 10, 1024, 0),
            (22, USER, "Beach", 10, 2048, 0),
            (23, SHARED, "Book club", None, None, 0),
            (24, USER, "Allotment", 10, 4096, 0),
        ],
        [],
    )
    assert [a["title"] for a in _albums(backup)[3:]] == ["Zoo", "Beach", "Allotment", "Book club", "Family"]
    assert {a["title"]: a["kind"] for a in _albums(backup)[3:]}["Family"] == "shared"


def test_photos_own_albums_and_deleted_albums_are_left_out(tmp_path):
    backup = _library(
        tmp_path,
        [
            (10, ROOT, None, None, None, 0),
            (11, PROJECTS_ROOT, None, None, None, 0),
            (12, PROGRESS, "progress-sync", None, None, 0),
            (13, 3572, "progress-ota-restore", None, None, 0),
            (14, 3573, "progress-fs-import", None, None, 0),
            (15, USER, "Old album", 10, 1024, 1),
            (16, USER, "Kept", 10, 2048, 0),
            (17, 1506, None, None, None, 0),  # an import session: no title
        ],
        [],
    )
    assert [a["title"] for a in _albums(backup)[3:]] == ["Kept"]


def test_an_album_in_a_deleted_folder_is_at_the_top_level(tmp_path):
    backup = _library(
        tmp_path,
        [(10, ROOT, None, None, None, 0), (11, FOLDER, "Gone", 10, 1024, 1), (12, USER, "Still here", 11, 1024, 0)],
        [],
    )
    assert [(a["title"], a["parent_id"]) for a in _albums(backup)[3:]] == [("Still here", None)]


def test_a_loop_of_folders_still_lists_every_album(tmp_path):
    backup = _library(
        tmp_path,
        [(11, FOLDER, "A", 12, 1024, 0), (12, FOLDER, "B", 11, 1024, 0), (13, USER, "C", 11, 2048, 0)],
        [],
    )
    albums = {a["title"]: a["parent_id"] for a in _albums(backup)[3:]}
    assert set(albums) == {"A", "B", "C"}
    assert None in albums.values()  # the loop is cut, so the tree has a top


def test_a_library_without_folder_columns_lists_albums_by_title(tmp_path):
    backup = _library(
        tmp_path,
        [(1, USER, "Trip", None, None, 0), (2, USER, "Birthday", None, None, 0), (3, PROGRESS, "progress-sync", None, None, 0)],
        [],
        album_columns=False,
    )
    assert [(a["title"], a["parent_id"], a["kind"]) for a in _albums(backup)[3:]] == [
        ("Birthday", None, "user"),
        ("Trip", None, "user"),
    ]
