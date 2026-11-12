"""
Podcast extraction from the Apple Podcasts library, MTLibrary.sqlite.

The library lives in the Podcasts app group container
(``AppDomainGroup-243LU875E5.groups.com.apple.podcasts``, path
``Documents/MTLibrary.sqlite``) on every version checked so far: iOS 13.3.1,
13.4.1, 16.1.2, 17.3 and 26. It is found through the manifest rather than by
that name, in case another version keeps it elsewhere.

What a backup holds:

- ``ZMTPODCAST``: one row per show the library knows about. Followed shows
  have ``ZSUBSCRIBED = 1``. From iOS 16, shows you have only played can be
  "implicitly followed" (``ZISIMPLICITLYFOLLOWED = 1``); they are not shows
  you chose to follow, so they are not ``followed`` here.
- ``ZMTEPISODE``: every episode the app has seen in each feed, with the
  enclosure (audio) URL, play count, playhead and last played date.

What it never holds: the downloaded audio. ``ZASSETURL`` points into
``/var/mobile/Media/Podcasts/`` (or ``ipod-library://`` for synced episodes),
and that folder is not part of an iPhone backup.

The table grows from about 49 columns (iOS 13) to about 100 (iOS 26), so
columns are checked with ``PRAGMA table_info`` before they are selected and a
missing one reads as ``None``.
"""

import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Iterable

from ios_backup_core.db import connect_readonly, get_database
from ios_backup_core.timestamps import apple_to_iso

PODCASTS_DOMAIN = "AppDomainGroup-243LU875E5.groups.com.apple.podcasts"
PODCASTS_DB_PATH = "Documents/MTLibrary.sqlite"

# ZPLAYSTATE values (Core Data enum in MTEpisode).
_PLAY_STATES = {0: "played", 1: "in_progress", 2: "unplayed"}

_NOT_READABLE = (
    "Podcasts are in this backup but could not be read. "
    "The backup may be incomplete or damaged."
)


def _text(value) -> str | None:
    """Return a TEXT value as str. Some backups store text columns as bytes."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8", errors="replace")
    value = str(value).strip()
    return value or None


def _int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flag(value) -> bool:
    return bool(_int(value))


def _date(value) -> str | None:
    if value is None or value == "":
        return None
    try:
        return apple_to_iso(float(value))
    except (TypeError, ValueError):
        return None


def _columns(cursor, table: str) -> set:
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def _select(columns: set, wanted: Iterable[str], table_alias: str = "") -> str:
    """SELECT list with the columns that exist; missing ones read as NULL."""
    prefix = f"{table_alias}." if table_alias else ""
    return ", ".join(
        f"{prefix}{name} AS {name}" if name in columns else f"NULL AS {name}"
        for name in wanted
    )


# Characters XML 1.0 can't hold at all, even escaped (C0 controls other than
# tab, newline and carriage return, lone surrogates, U+FFFE/U+FFFF).
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _xml_safe(value: str) -> str:
    return _XML_ILLEGAL.sub("", value)


def _downloaded_on_phone(asset_url: str | None) -> bool:
    """True when the phone had the episode's audio. The audio itself is never backed up."""
    return bool(asset_url) and asset_url.startswith(("file:", "ipod-library:"))


class PodcastExtractor:
    """Extracts Apple Podcasts shows and listening history from iOS backups."""

    PODCAST_DB_PATH = PODCASTS_DB_PATH

    def _find_database(self, backup):
        """Return (domain, path) of MTLibrary.sqlite in the manifest, or None."""
        try:
            entries = backup.list_files(path_like="%MTLibrary.sqlite") or []
        except Exception:
            entries = []
        # Prefer the Podcasts container, then the usual path, then anything.
        entries = [e for e in entries if (e.get("path") or "").endswith("MTLibrary.sqlite")]
        entries.sort(key=lambda e: (
            "com.apple.podcasts" not in (e.get("domain") or "").lower(),
            e.get("path") != PODCASTS_DB_PATH,
        ))
        if entries:
            return entries[0].get("domain") or PODCASTS_DOMAIN, entries[0]["path"]
        return None

    def _open(self, backup, result_key: str):
        """Open the library. Returns (conn, None) or (None, empty_or_error_result)."""
        found = self._find_database(backup)
        if not found:
            return None, {result_key: [], "empty_reason": "not_in_backup"}
        domain, path = found
        db_path = get_database(backup, path, domain=domain)
        if not db_path:
            return None, {result_key: [], "error": _NOT_READABLE}
        try:
            conn = connect_readonly(db_path, backup)
        except sqlite3.Error as e:
            return None, {result_key: [], "error": f"{_NOT_READABLE} ({e})"}
        conn.row_factory = sqlite3.Row
        # Titles and descriptions come from publishers' feeds: one value that
        # isn't valid UTF-8 must not fail the whole library.
        conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
        return conn, None

    # ------------------------------------------------------------------
    # Shows
    # ------------------------------------------------------------------

    _PODCAST_COLUMNS = (
        "Z_PK", "ZUUID", "ZTITLE", "ZAUTHOR", "ZPROVIDER", "ZITEMDESCRIPTION",
        "ZFEEDURL", "ZUPDATEDFEEDURL", "ZWEBPAGEURL", "ZIMAGEURL",
        "ZSTORECLEANURL", "ZSTORESHORTURL", "ZSTORECOLLECTIONID", "ZCATEGORY",
        "ZSUBSCRIBED", "ZISIMPLICITLYFOLLOWED", "ZHIDDEN", "ZADDEDDATE",
        "ZLASTDATEPLAYED", "ZLASTUNFOLLOWEDDATE", "ZSHOWTYPEINFEED", "ZSORTORDER",
    )

    def list_podcasts(self, backup) -> dict:
        """List every show in the Podcasts library.

        Returns ``{"podcasts": [...]}``. Each show has ``followed`` (the user
        chose to follow it: subscribed and not just implicitly followed),
        ``feed_url`` (the feed's newest known address, which is what a podcast
        app needs), plus episode counts.

        When there are none, ``podcasts`` is empty and ``empty_reason`` says why:

        - ``"not_in_backup"``: no MTLibrary.sqlite (Podcasts never used, or
          the app was removed).
        - ``"no_records"``: MTLibrary.sqlite exists but holds no shows.

        A library that is listed in the backup but can't be read is an
        ``error``, with no ``empty_reason``.
        """
        conn, early = self._open(backup, "podcasts")
        if early is not None:
            return early

        podcasts = []
        try:
            cursor = conn.cursor()
            tables = {r[0] for r in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "ZMTPODCAST" not in tables:
                return {"podcasts": [], "empty_reason": "no_records"}
            columns = _columns(cursor, "ZMTPODCAST")

            counts = {}
            if "ZMTEPISODE" in tables:
                ep_cols = _columns(cursor, "ZMTEPISODE")
                if "ZPODCAST" in ep_cols:
                    listened = self._listened_sql(ep_cols)
                    for row in cursor.execute(f"""
                        SELECT ZPODCAST AS pid, COUNT(*) AS n,
                               SUM(CASE WHEN {listened} THEN 1 ELSE 0 END) AS heard
                        FROM ZMTEPISODE GROUP BY ZPODCAST
                    """):
                        counts[row["pid"]] = (row["n"] or 0, row["heard"] or 0)

            order = "ZSORTORDER, ZTITLE" if "ZSORTORDER" in columns else "ZTITLE"
            rows = cursor.execute(
                f"SELECT {_select(columns, self._PODCAST_COLUMNS)} "
                f"FROM ZMTPODCAST ORDER BY {order}"
            ).fetchall()

            for row in rows:
                original_feed = _text(row["ZFEEDURL"])
                feed = _text(row["ZUPDATEDFEEDURL"]) or original_feed
                implicit = _flag(row["ZISIMPLICITLYFOLLOWED"])
                subscribed = _flag(row["ZSUBSCRIBED"])
                episode_count, listened_count = counts.get(row["Z_PK"], (0, 0))
                podcasts.append({
                    "id": row["Z_PK"],
                    "uuid": _text(row["ZUUID"]),
                    "title": _text(row["ZTITLE"]),
                    "author": _text(row["ZAUTHOR"]) or _text(row["ZPROVIDER"]),
                    "description": _text(row["ZITEMDESCRIPTION"]),
                    "category": _text(row["ZCATEGORY"]),
                    "feed_url": feed,
                    "original_feed_url": original_feed if original_feed != feed else None,
                    "webpage_url": _text(row["ZWEBPAGEURL"]),
                    "image_url": _text(row["ZIMAGEURL"]),
                    "store_url": _text(row["ZSTORECLEANURL"]) or _text(row["ZSTORESHORTURL"]),
                    "store_id": _int(row["ZSTORECOLLECTIONID"]) or None,
                    "show_type": _text(row["ZSHOWTYPEINFEED"]),
                    "followed": subscribed and not implicit,
                    "subscribed": subscribed,
                    "implicitly_followed": implicit,
                    "hidden": _flag(row["ZHIDDEN"]),
                    "added_date": _date(row["ZADDEDDATE"]),
                    "last_played_date": _date(row["ZLASTDATEPLAYED"]),
                    "unfollowed_date": _date(row["ZLASTUNFOLLOWEDDATE"]),
                    "episode_count": episode_count,
                    "listened_count": listened_count,
                })
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"podcasts": [], "error": f"{_NOT_READABLE} ({e})"}
        finally:
            conn.close()

        if not podcasts:
            return {"podcasts": [], "empty_reason": "no_records"}
        return {"podcasts": podcasts}

    # ------------------------------------------------------------------
    # Episodes
    # ------------------------------------------------------------------

    _EPISODE_COLUMNS = (
        "Z_PK", "ZPODCAST", "ZUUID", "ZGUID", "ZTITLE", "ZAUTHOR",
        "ZPUBDATE", "ZDURATION", "ZPLAYCOUNT", "ZPLAYHEAD", "ZPLAYSTATE",
        "ZLASTDATEPLAYED", "ZSAVED", "ZISBOOKMARKED", "ZASSETURL",
        "ZENCLOSUREURL", "ZWEBPAGEURL", "ZSEASONNUMBER", "ZEPISODENUMBER",
        "ZEPISODETYPE", "ZVIDEO", "ZFEEDDELETED", "ZISHIDDEN", "ZUSERDELETED",
        "ZITEMDESCRIPTIONWITHOUTHTML",
    )

    @staticmethod
    def _listened_sql(columns: set, alias: str = "") -> str:
        """SQL that is true when an episode shows signs of being listened to.

        ``ZPLAYSTATE = 0`` ("played") is not enough on its own: when you
        follow a show, the app marks its whole back catalogue as played
        (about 1,000 of 1,024 rows in one iOS 13 library). Play count, last
        played date and a saved playhead only come from listening.
        """
        p = f"{alias}." if alias else ""
        parts = []
        if "ZPLAYCOUNT" in columns:
            parts.append(f"COALESCE({p}ZPLAYCOUNT, 0) > 0")
        if "ZLASTDATEPLAYED" in columns:
            parts.append(f"COALESCE({p}ZLASTDATEPLAYED, 0) > 0")
        if "ZPLAYHEAD" in columns:
            parts.append(f"COALESCE({p}ZPLAYHEAD, 0) > 0")
        return "(" + " OR ".join(parts) + ")" if parts else "0"

    def list_episodes(
        self,
        backup,
        podcast_id: int | None = None,
        listened_only: bool = False,
        include_descriptions: bool = False,
    ) -> dict:
        """List episodes, newest first, with listening history.

        ``podcast_id`` limits the list to one show (the ``id`` from
        list_podcasts). ``listened_only`` keeps episodes with a play count,
        a last played date or a saved playhead, which is the listening
        history.

        Each episode has ``listened`` (as above), ``play_state``
        (``"played"``, ``"in_progress"`` or ``"unplayed"``, as the app shows
        it; see _listened_sql for why "played" alone is not history),
        ``saved``, and ``downloaded_on_phone``. The audio of a downloaded
        episode is not in the backup; ``enclosure_url`` is where the
        podcast publishes it.

        Empty results use ``empty_reason`` like list_podcasts.
        """
        conn, early = self._open(backup, "episodes")
        if early is not None:
            return early

        episodes = []
        try:
            cursor = conn.cursor()
            tables = {r[0] for r in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "ZMTEPISODE" not in tables:
                return {"episodes": [], "empty_reason": "no_records"}
            columns = _columns(cursor, "ZMTEPISODE")
            wanted = [c for c in self._EPISODE_COLUMNS
                      if include_descriptions or c != "ZITEMDESCRIPTIONWITHOUTHTML"]

            show_titles = {}
            if "ZMTPODCAST" in tables and "ZTITLE" in _columns(cursor, "ZMTPODCAST"):
                for row in cursor.execute("SELECT Z_PK, ZTITLE FROM ZMTPODCAST"):
                    show_titles[row[0]] = _text(row[1])

            where, params = [], []
            if podcast_id is not None:
                if "ZPODCAST" not in columns:
                    return {"episodes": [], "empty_reason": "no_records"}
                where.append("ZPODCAST = ?")
                params.append(int(podcast_id))
            if listened_only:
                where.append(self._listened_sql(columns))
            clause = f"WHERE {' AND '.join(where)}" if where else ""
            order = "ZPUBDATE DESC, Z_PK DESC" if "ZPUBDATE" in columns else "Z_PK DESC"

            rows = cursor.execute(
                f"SELECT {_select(columns, wanted)} FROM ZMTEPISODE {clause} ORDER BY {order}",
                params,
            ).fetchall()

            for row in rows:
                play_count = _int(row["ZPLAYCOUNT"]) or 0
                playhead = _float(row["ZPLAYHEAD"]) or 0.0
                last_played = _date(row["ZLASTDATEPLAYED"])
                asset = _text(row["ZASSETURL"])
                episode = {
                    "id": row["Z_PK"],
                    "podcast_id": row["ZPODCAST"],
                    "podcast_title": show_titles.get(row["ZPODCAST"]),
                    "uuid": _text(row["ZUUID"]),
                    "guid": _text(row["ZGUID"]),
                    "title": _text(row["ZTITLE"]),
                    "author": _text(row["ZAUTHOR"]),
                    "published_date": _date(row["ZPUBDATE"]),
                    "duration_seconds": _float(row["ZDURATION"]) or None,
                    "play_count": play_count,
                    "playhead_seconds": playhead or None,
                    "last_played_date": last_played,
                    "play_state": _PLAY_STATES.get(_int(row["ZPLAYSTATE"])),
                    "listened": bool(play_count or playhead or last_played),
                    "saved": _flag(row["ZSAVED"]) or _flag(row["ZISBOOKMARKED"]),
                    "downloaded_on_phone": _downloaded_on_phone(asset),
                    "enclosure_url": _text(row["ZENCLOSUREURL"]),
                    "webpage_url": _text(row["ZWEBPAGEURL"]),
                    "season": _int(row["ZSEASONNUMBER"]) or None,
                    "episode_number": _int(row["ZEPISODENUMBER"]) or None,
                    "episode_type": _text(row["ZEPISODETYPE"]),
                    "video": _flag(row["ZVIDEO"]),
                    "removed_from_feed": _flag(row["ZFEEDDELETED"]),
                    "hidden": _flag(row["ZISHIDDEN"]) or _flag(row["ZUSERDELETED"]),
                }
                if include_descriptions:
                    episode["description"] = _text(row["ZITEMDESCRIPTIONWITHOUTHTML"])
                episodes.append(episode)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"episodes": [], "error": f"{_NOT_READABLE} ({e})"}
        finally:
            conn.close()

        if not episodes:
            return {"episodes": [], "empty_reason": "no_records"}
        return {"episodes": episodes}


def podcasts_to_opml(
    podcasts: Iterable[dict],
    title: str = "Podcasts",
    date_created: str | None = None,
    followed_only: bool = True,
) -> str:
    """Write shows as an OPML 2.0 subscription list for another podcast app.

    ``podcasts`` are dicts from list_podcasts (or anything with ``title``,
    ``feed_url``, ``webpage_url`` and ``followed``). Shows without a feed URL
    are left out, since a podcast app can't subscribe without one; with
    ``followed_only`` (the default) so are shows the user didn't follow.
    ``date_created`` is an RFC 822 date string, written as given.

    ElementTree escapes ``&``, ``<`` and quotes in titles and URLs.
    """
    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = _xml_safe(title)
    if date_created:
        ET.SubElement(head, "dateCreated").text = _xml_safe(date_created)
    body = ET.SubElement(opml, "body")
    for show in podcasts:
        feed = show.get("feed_url")
        if not feed or (followed_only and not show.get("followed")):
            continue
        name = _xml_safe(show.get("title") or feed)
        attrs = {"type": "rss", "text": name, "title": name, "xmlUrl": _xml_safe(feed)}
        if show.get("webpage_url"):
            attrs["htmlUrl"] = _xml_safe(show["webpage_url"])
        ET.SubElement(body, "outline", attrs)
    ET.indent(opml, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(opml, encoding="unicode") + "\n"
