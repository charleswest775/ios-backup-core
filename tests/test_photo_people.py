"""The People album: the groups the Photos app made of the faces it found, and photos by person.

The tables follow the iOS 17–26 Photos.sqlite (ZDETECTEDFACE.ZPERSONFORFACE/ZASSETFORFACE)
and the iOS 13–16 one (ZPERSON/ZASSET), whose columns the harvested schemas in
backupgen/schemas show. Most groups are never named, and ZPERSON.ZFACECOUNT can be stale
on a real phone (it's 99 for everyone here on purpose), so counts come from the faces.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.photos import FAVORITES, PhotoExtractor


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


# Assets: (pk, favorite, hidden, trashed). 1–4 plain, 5 a favorite, 6 hidden, 7 in Recently Deleted.
ASSETS = [(1, 0, 0, 0), (2, 0, 0, 0), (3, 0, 0, 0), (4, 0, 0, 0), (5, 1, 0, 0), (6, 0, 1, 0), (7, 0, 0, 1)]
# People: (pk, full name, display name, uri, key face, merged into, detection type)
PEOPLE = [
    (10, "Ada Lovelace", "Ada", "AB12-CD34:ABPerson", 103, None, 1),  # named, linked to a card
    (11, None, None, None, None, None, 1),                            # never named
    (12, None, None, None, None, 10, 1),                              # merged into Ada
    (13, None, None, None, None, None, 3),                            # a dog
    (14, None, None, None, None, None, 1),                            # only on hidden and deleted photos
]
# Faces: (pk, person, asset, x, y, size, hidden, detection type)
FACES = [
    (100, 10, 1, 0.4, 0.4, 0.10, 0, 1),
    (101, 10, 2, 0.3, 0.5, 0.25, 0, 1),
    (102, 10, 2, 0.7, 0.5, 0.05, 0, 1),  # Ada twice in one photo (a mirror): one photo
    (103, 10, 6, 0.5, 0.5, 0.30, 0, 1),  # Ada's key face, on a hidden photo
    (104, 11, 2, 0.6, 0.4, 0.12, 0, 1),
    (105, 11, 3, 0.5, 0.5, 0.20, 0, 1),
    (106, 11, 4, 0.5, 0.5, 0.20, 1, 1),  # hidden from People
    (107, 12, 4, 0.2, 0.3, 0.15, 0, 1),  # the merged group's face: Ada's
    (108, 13, 3, 0.8, 0.8, 0.20, 0, 3),
    (109, 14, 6, 0.5, 0.5, 0.20, 0, 1),
    (110, 14, 7, 0.5, 0.5, 0.20, 0, 1),
    (111, None, 1, 0.1, 0.1, 0.05, 0, 1),  # a face Photos put in no group
    (112, 11, 5, 0.5, 0.5, 0.20, 0, 1),
]


def _library(tmp_path, ios17=True, assets_table="ZASSET", faces=FACES, people=PEOPLE, with_faces=True):
    path = str(tmp_path / "Photos.sqlite")
    conn = sqlite3.connect(path)
    conn.execute(
        f"CREATE TABLE {assets_table} (Z_PK INTEGER PRIMARY KEY, ZUUID VARCHAR, ZFILENAME VARCHAR, ZDIRECTORY VARCHAR, "
        "ZKIND INTEGER, ZDATECREATED TIMESTAMP, ZFAVORITE INTEGER, ZHIDDEN INTEGER, ZTRASHEDSTATE INTEGER)"
    )
    for pk, favorite, hidden, trashed in ASSETS:
        conn.execute(f"INSERT INTO {assets_table} VALUES (?, ?, ?, 'DCIM/100APPLE', 0, ?, ?, ?, ?)",
                     (pk, f"A-{pk}", f"IMG_{pk:04}.HEIC", 700_000_000.0 + pk, favorite, hidden, trashed))
    if with_faces:
        person, asset = ("ZPERSONFORFACE", "ZASSETFORFACE") if ios17 else ("ZPERSON", "ZASSET")
        detection = ", ZDETECTIONTYPE INTEGER" if ios17 else ""
        conn.execute(
            "CREATE TABLE ZPERSON (Z_PK INTEGER PRIMARY KEY, ZFULLNAME VARCHAR, ZDISPLAYNAME VARCHAR, "
            f"ZPERSONURI VARCHAR, ZKEYFACE INTEGER, ZMERGETARGETPERSON INTEGER, ZFACECOUNT INTEGER{detection})"
        )
        conn.execute(
            f"CREATE TABLE ZDETECTEDFACE (Z_PK INTEGER PRIMARY KEY, {person} INTEGER, {asset} INTEGER, "
            f"ZCENTERX FLOAT, ZCENTERY FLOAT, ZSIZE FLOAT, ZHIDDEN INTEGER{detection})"
        )
        for row in people:
            values = row if ios17 else row[:-1]
            conn.execute(f"INSERT INTO ZPERSON VALUES ({', '.join('?' * (len(values) + 1))})",
                         (*values[:6], 99, *values[6:]))  # ZFACECOUNT stale on purpose
        for row in faces:
            values = row if ios17 else row[:-1]
            conn.execute(f"INSERT INTO ZDETECTEDFACE VALUES ({', '.join('?' * len(values))})", values)
    conn.commit()
    conn.close()
    return _FakeBackup(path)


def _people(backup, **kwargs):
    result = PhotoExtractor().list_people(backup, **kwargs)
    assert result["source"] == "photos_sqlite", result
    return result["people"]


def _uuids(backup, person_ids, match="any", album_id=None):
    result = PhotoExtractor().list_photos(backup, person_ids=person_ids, person_match=match, album_id=album_id)
    assert result["source"] == "photos_sqlite", result
    assert result["total"] == len(result["photos"])
    return sorted(p["uuid"] for p in result["photos"])


@pytest.mark.parametrize("ios17", [True, False], ids=["ios17-26", "ios13-16"])
def test_people_are_photos_groups_with_their_photo_counts(tmp_path, ios17):
    backup = _library(tmp_path, ios17=ios17)
    people = _people(backup, include_assets=True)
    pets = [] if ios17 else [("13", None, 1)]  # before iOS 17 there's no detection type to leave the dog out by
    assert sorted((p["id"], p["name"], p["photo_count"]) for p in people) == sorted([
        ("10", "Ada Lovelace", 3),  # assets 1 and 2, and 4 through the merged group; not the hidden 6
        ("11", None, 3),            # 2, 3 and the favorite 5; the face on 4 is hidden from People
        *pets,
    ])
    ada = next(p for p in people if p["id"] == "10")
    assert ada["contact_uri"] == "AB12-CD34:ABPerson"
    assert sorted(ada["asset_uuids"]) == ["A-1", "A-2", "A-4"]
    assert people[0]["photo_count"] >= people[-1]["photo_count"]


def test_most_photos_first_then_named_first(tmp_path):
    people = _people(_library(tmp_path))
    assert [p["id"] for p in people] == ["10", "11"]  # three photos each: the named one first


def test_key_face_is_photos_pick_when_visible_else_the_biggest(tmp_path):
    people = {p["id"]: p for p in _people(_library(tmp_path))}
    # Ada's key face (103) is on a hidden photo, so her biggest visible face stands in.
    assert people["10"]["key_face"] == {"asset_uuid": "A-2", "center_x": 0.3, "center_y": 0.5, "size": 0.25}
    visible_key = tmp_path / "visible-key"
    visible_key.mkdir()
    people = {p["id"]: p for p in _people(_library(visible_key, people=[(10, "Ada", None, None, 100, None, 1)]))}
    assert people["10"]["key_face"]["asset_uuid"] == "A-1"  # 100, though smaller than 101


def test_person_faces_best_first(tmp_path):
    backup = _library(tmp_path)
    faces = PhotoExtractor().list_person_faces(backup, "10")
    assert faces["source"] == "photos_sqlite"
    assert [(f["asset_uuid"], f["size"]) for f in faces["faces"]] == [
        ("A-2", 0.25), ("A-4", 0.15), ("A-1", 0.10), ("A-2", 0.05),
    ]
    assert PhotoExtractor().list_person_faces(backup, "999")["faces"] == []


@pytest.mark.parametrize("ios17", [True, False], ids=["ios17-26", "ios13-16"])
def test_photos_of_any_or_all_of_these_people(tmp_path, ios17):
    backup = _library(tmp_path, ios17=ios17)
    assert _uuids(backup, ["10"]) == ["A-1", "A-2", "A-4"]
    assert _uuids(backup, ["11"]) == ["A-2", "A-3", "A-5"]
    assert _uuids(backup, ["10", "11"]) == ["A-1", "A-2", "A-3", "A-4", "A-5"]
    assert _uuids(backup, ["10", "11"], match="all") == ["A-2"]


def test_person_filter_counts_match_the_people_list(tmp_path):
    backup = _library(tmp_path)
    for person in _people(backup):
        assert len(_uuids(backup, [person["id"]])) == person["photo_count"]


def test_a_merged_groups_own_id_is_the_person_it_went_into(tmp_path):
    backup = _library(tmp_path)
    assert _uuids(backup, ["12"]) == _uuids(backup, ["10"]) == ["A-1", "A-2", "A-4"]
    assert "12" not in [p["id"] for p in _people(backup)]
    assert len(PhotoExtractor().list_person_faces(backup, "12")["faces"]) == 4


def test_person_filter_narrows_an_album(tmp_path):
    assert _uuids(_library(tmp_path), ["11"], album_id=FAVORITES) == ["A-5"]


def test_unknown_people_match_nothing(tmp_path):
    backup = _library(tmp_path)
    assert _uuids(backup, ["999"]) == []
    assert _uuids(backup, ["not-a-number"]) == []
    assert _uuids(backup, ["10", "999"], match="all") == []
    assert _uuids(backup, ["10", "999"]) == ["A-1", "A-2", "A-4"]


def test_no_person_filter_lists_all_photos(tmp_path):
    result = PhotoExtractor().list_photos(_library(tmp_path), person_ids=[])
    assert sorted(p["uuid"] for p in result["photos"]) == ["A-1", "A-2", "A-3", "A-4", "A-5"]


def test_before_ios_14_the_assets_are_zgenericasset(tmp_path):
    backup = _library(tmp_path, ios17=False, assets_table="ZGENERICASSET")
    assert {p["id"] for p in _people(backup)} == {"10", "11", "13"}
    assert _uuids(backup, ["10"]) == ["A-1", "A-2", "A-4"]


def test_a_library_without_people_tables_has_no_people(tmp_path):
    backup = _library(tmp_path, with_faces=False)
    assert _people(backup) == []
    assert _uuids(backup, ["10"]) == []
    assert PhotoExtractor().list_person_faces(backup, "10")["faces"] == []


def test_a_merge_loop_doesnt_hang(tmp_path):
    people = [(10, "Ada", None, None, None, 11, 1), (11, None, None, None, None, 10, 1)]
    faces = [(100, 10, 1, 0.5, 0.5, 0.1, 0, 1), (101, 11, 2, 0.5, 0.5, 0.1, 0, 1)]
    backup = _library(tmp_path, people=people, faces=faces)
    listed = _people(backup)
    assert sum(p["photo_count"] for p in listed) == 2


def test_no_photos_library(tmp_path):
    assert PhotoExtractor().list_people(_FakeBackup(str(tmp_path / "missing.sqlite")))["source"] == "unavailable"
