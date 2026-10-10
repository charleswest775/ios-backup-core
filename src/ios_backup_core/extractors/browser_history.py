"""
Browser history extraction from Safari and Firefox iOS backups.

Extracted from openextract/python/browser_history.py:BrowserHistoryExtractor.
Changes:
  - export_browser_history_csv() removed — UI concern
  - apple_date_to_iso import replaced with ios_backup_core.timestamps.apple_to_iso
  - Inline Firefox timestamp conversion replaced with firefox_to_iso()
  - Diagnostic stderr print kept (non-fatal, informs callers about raw data ranges)
"""

import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

from ios_backup_core.timestamps import apple_to_iso, firefox_to_iso


class BrowserHistoryExtractor:
    """Extracts browser history from Safari and Firefox in iOS backups."""

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

    def _ensure_wal(self, backup, relative_path: str, domain: str) -> None:
        """Extract WAL/SHM files alongside the main DB so SQLite can merge them."""
        backup.get_file(relative_path + "-wal", domain=domain)
        backup.get_file(relative_path + "-shm", domain=domain)

    def _find_all_firefox_dbs(self, backup) -> list:
        """Return list of (db_path, schema) for all Firefox history databases found."""
        found = []
        for domain in self.FIREFOX_DOMAINS:
            for path in self.FIREFOX_LEGACY_PATHS:
                db_path = backup.get_file(path, domain=domain)
                if db_path:
                    self._ensure_wal(backup, path, domain)
                    found.append((db_path, "legacy"))
                    break
            for path in self.FIREFOX_PLACES_PATHS:
                db_path = backup.get_file(path, domain=domain)
                if db_path:
                    self._ensure_wal(backup, path, domain)
                    found.append((db_path, "places"))
                    break
        if not found:
            try:
                for pattern, schema in [("%browser.db%", "legacy"), ("%places.db%", "places")]:
                    for f in backup.list_files(path_like=pattern):
                        domain = f.get("domain", "")
                        if "mozilla" in domain.lower() or "firefox" in domain.lower():
                            db_path = backup.get_file(f["path"], domain=domain)
                            if db_path:
                                self._ensure_wal(backup, f["path"], domain)
                                found.append((db_path, schema))
            except Exception:
                pass
        return found

    def _has_safari_visits(self, backup) -> bool:
        db_path = backup.get_file(self.SAFARI_HISTORY_PATH, domain=self.SAFARI_DOMAIN)
        if not db_path:
            return False
        self._ensure_wal(backup, self.SAFARI_HISTORY_PATH, self.SAFARI_DOMAIN)
        try:
            conn = sqlite3.connect(db_path)
            count = conn.execute("SELECT COUNT(*) FROM history_visits").fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False

    def _has_firefox_visits(self, backup) -> bool:
        for db_path, schema in self._find_all_firefox_dbs(backup):
            try:
                conn = sqlite3.connect(db_path)
                table = "visits" if schema == "legacy" else "moz_historyvisits"
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                conn.close()
                if count > 0:
                    return True
            except Exception:
                pass
        return False

    def has_browser_history(self, backup) -> dict:
        """Quick probe — returns True only when history is actually readable."""
        safari = self._has_safari_visits(backup)
        firefox = self._has_firefox_visits(backup)
        return {"safari": safari, "firefox": firefox, "has_any": safari or firefox}

    def list_browser_history(
        self,
        backup,
        browser: str = "all",
        offset: int = 0,
        limit: int = 0,
    ) -> dict:
        """List browser history visits, optionally filtered by browser."""
        all_visits = []
        browsers_found = []

        if browser in ("all", "safari"):
            safari_visits, _ = self._get_safari_history(backup)
            if safari_visits:
                all_visits.extend(safari_visits)
                browsers_found.append("safari")

        if browser in ("all", "firefox"):
            firefox_visits, _ = self._get_firefox_history(backup)
            if firefox_visits:
                all_visits.extend(firefox_visits)
                browsers_found.append("firefox")

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
            "browsers_found": browsers_found,
        }

    def _extract_domain(self, url: str) -> str:
        try:
            netloc = urlparse(url).netloc
            return netloc if netloc else url
        except Exception:
            return url

    def _get_safari_history(self, backup) -> tuple:
        """Get Safari browser history. Returns (visits_list, total_count)."""
        db_path = backup.get_file(self.SAFARI_HISTORY_PATH, domain=self.SAFARI_DOMAIN)
        if not db_path:
            return [], 0
        self._ensure_wal(backup, self.SAFARI_HISTORY_PATH, self.SAFARI_DOMAIN)

        visits = []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = TRUE")
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute("PRAGMA cache_size = -10000")
            conn.execute("PRAGMA temp_store = MEMORY")
            cursor = conn.cursor()

            try:
                diag = cursor.execute(
                    "SELECT COUNT(*), MIN(visit_time), MAX(visit_time) FROM history_visits"
                ).fetchone()
                print(
                    f"[browser_history] Safari diag: total_rows={diag[0]}, "
                    f"min_visit_time={diag[1]}, max_visit_time={diag[2]}, "
                    f"min_converted={apple_to_iso(diag[1])}, "
                    f"max_converted={apple_to_iso(diag[2])}",
                    file=sys.stderr, flush=True,
                )
            except Exception as de:
                print(f"[browser_history] Safari diag error: {de}", file=sys.stderr, flush=True)

            rows = cursor.execute("""
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

            for row in rows:
                url = row["url"] or ""
                # Not domain_expansion: Safari stores a short label there
                # ("nhl" for www.nhl.com, "support.garmin"), not a host name.
                domain = self._extract_domain(url)
                visit_date = apple_to_iso(row["visit_time"])

                visits.append({
                    "visit_id": f"s_{row['visit_id']}",
                    "url": url,
                    "title": row["title"] or "",
                    "domain": domain,
                    "visit_date": visit_date,
                    "browser": "safari",
                    "visit_count": row["visit_count"],
                })

            conn.close()
        except Exception as e:
            print(f"[browser_history] Safari parse error: {e}", file=sys.stderr, flush=True)

        return visits, len(visits)

    def _read_firefox_db(self, db_path: str, schema: str, id_prefix: str) -> list:
        """Read visits from a single Firefox database."""
        visits = []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = TRUE")
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute("PRAGMA cache_size = -10000")
            conn.execute("PRAGMA temp_store = MEMORY")
            cursor = conn.cursor()

            if schema == "legacy":
                # Timestamps: microseconds since Unix epoch
                rows = cursor.execute("""
                    SELECT v.id AS visit_id, h.url, h.title, v.date, 1000000 AS divisor
                    FROM visits v
                    JOIN history h ON v.siteID = h.id
                    WHERE h.is_deleted = 0
                    ORDER BY v.date DESC
                """).fetchall()
            else:
                # Timestamps: milliseconds since Unix epoch
                rows = cursor.execute("""
                    SELECT v.id AS visit_id, p.url, p.title, v.visit_date AS date, 1000 AS divisor
                    FROM moz_historyvisits v
                    JOIN moz_places p ON v.place_id = p.id
                    WHERE p.hidden = 0
                    ORDER BY v.visit_date DESC
                """).fetchall()

            for row in rows:
                url = row["url"] or ""
                visit_date = firefox_to_iso(row["date"], divisor=row["divisor"])
                visits.append({
                    "visit_id": f"{id_prefix}_{row['visit_id']}",
                    "url": url,
                    "title": row["title"] or "",
                    "domain": self._extract_domain(url),
                    "visit_date": visit_date,
                    "browser": "firefox",
                    "visit_count": None,
                })

            conn.close()
        except Exception as e:
            print(
                f"[browser_history] Firefox parse error ({schema}): {e}",
                file=sys.stderr, flush=True,
            )
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
