"""
Browser history extraction from iOS backups.

Supported browsers:
  - Safari   — HomeDomain Library/Safari/History.db, plus any per-profile
               History.db files (Safari profiles, iOS 17+)
  - Firefox  — legacy browser.db and places.db in the Mozilla app groups
  - Chrome, Edge, Brave — Chromium "History" databases in each app's domain

Each visit is returned as a dict with a stable shape (see _visit()). Parse
failures are collected in an ``errors`` list instead of being swallowed, so
callers can tell "no history" apart from "history we couldn't read".
"""

import sqlite3
import sys
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from ios_backup_core.backup import open_database
from ios_backup_core.timestamps import apple_to_iso, firefox_to_iso, webkit_to_iso

# Chromium visit transition core types for iframe loads. These are not page
# visits the user made, so they are left out (same as Chrome's history page).
_CHROMIUM_SUBFRAME_TRANSITIONS = (3, 4)  # AUTO_SUBFRAME, MANUAL_SUBFRAME

UNENCRYPTED_SAFARI_NOTICE = (
    "This backup isn't encrypted, and iOS leaves Safari history out of "
    "unencrypted backups. To include it, turn on \"Encrypt local backup\" "
    "and back up the iPhone again."
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = TRUE")
    conn.execute("PRAGMA cache_size = -10000")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def _tables(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _extract_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc
        return netloc if netloc else url
    except Exception:
        return url


def _visit(visit_id: str, url: str, title: str, domain: str,
           visit_date: Optional[str], browser: str, visit_count) -> dict:
    return {
        "visit_id": visit_id,
        "url": url,
        "title": title,
        "domain": domain,
        "visit_date": visit_date,
        "browser": browser,
        "visit_count": visit_count,
    }


class BrowserHistoryExtractor:
    """Extracts browser history from Safari, Firefox and Chromium browsers."""

    SAFARI_HISTORY_PATH = "Library/Safari/History.db"
    SAFARI_DOMAIN = "HomeDomain"

    FIREFOX_LEGACY_PATHS = [
        "profile.profile/browser.db",
        "Library/browser.db",
    ]
    FIREFOX_PLACES_PATHS = [
        "profile.profile/places.db",
        "Library/places.db",
    ]
    FIREFOX_DOMAINS = [
        "AppDomainGroup-group.org.mozilla.ios.Firefox",
        "AppDomainGroup-group.org.mozilla.ios.Fennec",
    ]

    # (browser key, lowercase bundle-id fragment of the app's backup domain).
    # All of these ship Chromium's History schema (urls + visits tables).
    CHROMIUM_BROWSERS = [
        ("chrome", "com.google.chrome.ios"),
        ("edge", "com.microsoft.msedge"),
        ("brave", "com.brave.ios.browser"),
    ]

    BROWSERS = ["safari", "firefox"] + [key for key, _ in CHROMIUM_BROWSERS]

    # ── Discovery ────────────────────────────────────────────────────────────

    def _find_safari_dbs(self, backup) -> list:
        """Return [db_path] for the default Safari history and any profile copies."""
        candidates = [(self.SAFARI_HISTORY_PATH, self.SAFARI_DOMAIN)]
        try:
            for f in backup.list_files(path_like="%Safari%History.db"):
                domain = f.get("domain", "")
                if domain == "HomeDomain" or "safari" in domain.lower():
                    candidates.append((f["path"], domain))
        except Exception:
            pass

        found, seen = [], set()
        for path, domain in candidates:
            if (domain, path) in seen:
                continue
            seen.add((domain, path))
            db_path = open_database(backup, path, domain)
            if db_path:
                found.append(db_path)
        return found

    def _find_all_firefox_dbs(self, backup) -> list:
        """Return list of (db_path, schema) for all Firefox history databases found."""
        found = []
        for domain in self.FIREFOX_DOMAINS:
            for path in self.FIREFOX_LEGACY_PATHS:
                db_path = open_database(backup, path, domain)
                if db_path:
                    found.append((db_path, "legacy"))
                    break
            for path in self.FIREFOX_PLACES_PATHS:
                db_path = open_database(backup, path, domain)
                if db_path:
                    found.append((db_path, "places"))
                    break
        if not found:
            try:
                for pattern, schema in [("%browser.db", "legacy"), ("%places.db", "places")]:
                    for f in backup.list_files(path_like=pattern):
                        domain = f.get("domain", "")
                        if "mozilla" in domain.lower() or "firefox" in domain.lower():
                            db_path = open_database(backup, f["path"], domain)
                            if db_path:
                                found.append((db_path, schema))
            except Exception:
                pass
        return found

    def _find_chromium_dbs(self, backup) -> dict:
        """Return {browser_key: [db_path, ...]} for Chromium-based browsers.

        Chromium keeps one History file per profile, e.g.
        Library/Application Support/Google/Chrome/Default/History. Rather than
        hard-code per-app paths, find every file named History in a known
        browser's domain; the schema check at read time rejects anything else.
        """
        found: dict = {}
        try:
            files = backup.list_files(path_like="%/History")
        except Exception:
            return found
        for f in files:
            domain = f.get("domain", "")
            lowered = domain.lower()
            for key, fragment in self.CHROMIUM_BROWSERS:
                if fragment in lowered:
                    db_path = open_database(backup, f["path"], domain)
                    if db_path:
                        found.setdefault(key, []).append(db_path)
                    break
        return found

    # ── Probe ────────────────────────────────────────────────────────────────

    @staticmethod
    def _has_rows(db_path: str, table: str) -> bool:
        try:
            conn = sqlite3.connect(db_path)
            try:
                return conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
            finally:
                conn.close()
        except Exception:
            return False

    def has_browser_history(self, backup) -> dict:
        """Quick probe — a browser counts only when its history is actually readable.

        Returns one boolean per browser key, plus ``browsers`` (the keys that
        have history), ``has_any``, and ``notice`` — a user-facing explanation
        when Safari history is missing because the backup isn't encrypted.
        """
        result = {key: False for key in self.BROWSERS}
        result["safari"] = any(
            self._has_rows(p, "history_visits") for p in self._find_safari_dbs(backup)
        )
        result["firefox"] = any(
            self._has_rows(p, "visits" if schema == "legacy" else "moz_historyvisits")
            for p, schema in self._find_all_firefox_dbs(backup)
        )
        for key, paths in self._find_chromium_dbs(backup).items():
            result[key] = any(self._has_rows(p, "visits") for p in paths)

        browsers = [key for key in self.BROWSERS if result[key]]
        result["browsers"] = browsers
        result["has_any"] = bool(browsers)
        result["notice"] = self._notice(backup, result["safari"])
        return result

    @staticmethod
    def _notice(backup, has_safari: bool) -> Optional[str]:
        if has_safari or getattr(backup, "encrypted", True):
            return None
        return UNENCRYPTED_SAFARI_NOTICE

    # ── Listing ──────────────────────────────────────────────────────────────

    def list_browser_history(
        self,
        backup,
        browser: str = "all",
        offset: int = 0,
        limit: int = 0,
    ) -> dict:
        """List browser history visits, optionally filtered to one browser key."""
        all_visits = []
        browsers_found = []
        errors: list = []

        def want(key: str) -> bool:
            return browser in ("all", key)

        if want("safari"):
            visits = self._get_safari_history(backup, errors)
            if visits:
                all_visits.extend(visits)
                browsers_found.append("safari")

        if want("firefox"):
            visits, _ = self._get_firefox_history(backup)
            if visits:
                all_visits.extend(visits)
                browsers_found.append("firefox")

        if any(want(key) for key, _ in self.CHROMIUM_BROWSERS):
            for key, paths in self._find_chromium_dbs(backup).items():
                if not want(key):
                    continue
                visits = []
                for i, db_path in enumerate(paths):
                    prefix = key[0] if i == 0 else f"{key[0]}{i}"
                    visits.extend(self._read_chromium_db(db_path, key, prefix, errors))
                if visits:
                    all_visits.extend(visits)
                    browsers_found.append(key)

        all_visits.sort(key=lambda v: v.get("visit_date") or "")

        # Consolidate identical URLs visited within 120 seconds of each other.
        consolidated = []
        last_kept: dict = {}
        for v in all_visits:
            headline = (v.get("title") or v.get("domain") or v.get("url") or "").strip()
            visit_date = v.get("visit_date")
            if visit_date and headline:
                try:
                    ts = datetime.fromisoformat(visit_date.replace("Z", "+00:00"))
                    prev = last_kept.get(headline)
                    if prev is not None and (ts - prev).total_seconds() < 120:
                        continue
                    last_kept[headline] = ts
                except Exception:
                    pass
            consolidated.append(v)

        consolidated.reverse()

        total = len(consolidated)
        if limit:
            paged = consolidated[offset:offset + limit]
        else:
            paged = consolidated[offset:]

        return {
            "visits": paged,
            "total": total,
            "offset": offset,
            "limit": limit,
            "browsers_found": [key for key in self.BROWSERS if key in browsers_found],
            "errors": errors,
            "notice": self._notice(backup, "safari" in browsers_found),
        }

    # ── Safari ───────────────────────────────────────────────────────────────

    def _get_safari_history(self, backup, errors: list) -> list:
        visits = []
        for i, db_path in enumerate(self._find_safari_dbs(backup)):
            prefix = "s" if i == 0 else f"s{i}"
            visits.extend(self._read_safari_db(db_path, prefix, errors))
        # A visit synced into several profile databases shows up once.
        seen, deduped = set(), []
        for v in visits:
            key = (v["url"], v["visit_date"])
            if key not in seen:
                seen.add(key)
                deduped.append(v)
        return deduped

    def _read_safari_db(self, db_path: str, id_prefix: str, errors: list) -> list:
        visits = []
        try:
            conn = _connect(db_path)
            try:
                if not {"history_visits", "history_items"} <= _tables(conn):
                    errors.append({
                        "browser": "safari",
                        "message": "The history database has a layout this version doesn't recognize.",
                    })
                    return []
                rows = conn.execute("""
                    SELECT
                        hv.id AS visit_id,
                        hi.url,
                        hv.title,
                        hi.domain_expansion,
                        hv.visit_time,
                        hi.visit_count
                    FROM history_visits hv
                    JOIN history_items hi ON hv.history_item = hi.id
                    ORDER BY hv.visit_time DESC
                """).fetchall()
            finally:
                conn.close()
        except Exception as e:
            print(f"[browser_history] Safari parse error: {e}", file=sys.stderr, flush=True)
            errors.append({"browser": "safari", "message": f"Couldn't read the history database ({e})."})
            return []

        for row in rows:
            url = row["url"] or ""
            visits.append(_visit(
                visit_id=f"{id_prefix}_{row['visit_id']}",
                url=url,
                title=row["title"] or "",
                domain=row["domain_expansion"] or _extract_domain(url),
                visit_date=apple_to_iso(row["visit_time"]),
                browser="safari",
                visit_count=row["visit_count"],
            ))
        return visits

    # ── Chromium (Chrome, Edge, Brave) ───────────────────────────────────────

    def _read_chromium_db(self, db_path: str, browser: str, id_prefix: str,
                          errors: list) -> list:
        visits = []
        try:
            conn = _connect(db_path)
            try:
                if not {"urls", "visits"} <= _tables(conn):
                    # A file named History that isn't Chromium's — not an error.
                    return []
                where = []
                if "hidden" in _columns(conn, "urls"):
                    where.append("u.hidden = 0")
                if "transition" in _columns(conn, "visits"):
                    subframes = ", ".join(str(t) for t in _CHROMIUM_SUBFRAME_TRANSITIONS)
                    where.append(f"(v.transition & 255) NOT IN ({subframes})")
                where_sql = ("WHERE " + " AND ".join(where)) if where else ""
                rows = conn.execute(f"""
                    SELECT v.id AS visit_id, u.url, u.title, v.visit_time, u.visit_count
                    FROM visits v
                    JOIN urls u ON v.url = u.id
                    {where_sql}
                    ORDER BY v.visit_time DESC
                """).fetchall()
            finally:
                conn.close()
        except Exception as e:
            print(f"[browser_history] {browser} parse error: {e}", file=sys.stderr, flush=True)
            errors.append({"browser": browser, "message": f"Couldn't read the history database ({e})."})
            return []

        for row in rows:
            url = row["url"] or ""
            visits.append(_visit(
                visit_id=f"{id_prefix}_{row['visit_id']}",
                url=url,
                title=row["title"] or "",
                domain=_extract_domain(url),
                visit_date=webkit_to_iso(row["visit_time"]),
                browser=browser,
                visit_count=row["visit_count"],
            ))
        return visits

    # ── Firefox ──────────────────────────────────────────────────────────────

    def _read_firefox_db(self, db_path: str, schema: str, id_prefix: str) -> list:
        """Read visits from a single Firefox database."""
        visits = []
        try:
            conn = _connect(db_path)
            try:
                if schema == "legacy":
                    # Timestamps: microseconds since Unix epoch
                    rows = conn.execute("""
                        SELECT v.id AS visit_id, h.url, h.title, v.date, 1000000 AS divisor
                        FROM visits v
                        JOIN history h ON v.siteID = h.id
                        WHERE h.is_deleted = 0
                        ORDER BY v.date DESC
                    """).fetchall()
                else:
                    # Timestamps: milliseconds since Unix epoch
                    rows = conn.execute("""
                        SELECT v.id AS visit_id, p.url, p.title, v.visit_date AS date,
                               1000 AS divisor
                        FROM moz_historyvisits v
                        JOIN moz_places p ON v.place_id = p.id
                        WHERE p.hidden = 0
                        ORDER BY v.visit_date DESC
                    """).fetchall()
            finally:
                conn.close()
        except Exception as e:
            print(
                f"[browser_history] Firefox parse error ({schema}): {e}",
                file=sys.stderr, flush=True,
            )
            return []

        for row in rows:
            url = row["url"] or ""
            visits.append(_visit(
                visit_id=f"{id_prefix}_{row['visit_id']}",
                url=url,
                title=row["title"] or "",
                domain=_extract_domain(url),
                visit_date=firefox_to_iso(row["date"], divisor=row["divisor"]),
                browser="firefox",
                visit_count=None,
            ))
        return visits

    def _get_firefox_history(self, backup) -> tuple:
        """Get Firefox browser history from all databases."""
        dbs = self._find_all_firefox_dbs(backup)

        places_visits = []
        legacy_visits = []

        for db_path, schema in dbs:
            visits = self._read_firefox_db(db_path, schema, schema[0])
            if schema == "places":
                places_visits.extend(visits)
            else:
                legacy_visits.extend(visits)

        if places_visits and legacy_visits:
            dated = [v["visit_date"] for v in places_visits if v["visit_date"]]
            cutoff = min(dated) if dated else None
            if cutoff:
                legacy_visits = [v for v in legacy_visits
                                 if v["visit_date"] and v["visit_date"] < cutoff]

        seen: set = set()
        deduped = []
        for v in places_visits + legacy_visits:
            minute_key = v["visit_date"][:16] if v["visit_date"] else None
            key = (v["url"], minute_key)
            if key not in seen:
                seen.add(key)
                deduped.append(v)

        return deduped, len(deduped)
