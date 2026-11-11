"""
Podcast extraction from MTLibrary.sqlite.

"""

import sqlite3

from ios_backup_core.db import connect_readonly, get_database


class PodcastExtractor:
    """Extracts podcast subscriptions from iOS backups."""

    PODCAST_DB_PATH = "Documents/MTLibrary.sqlite"

    def list_podcasts(self, backup) -> dict:
        """List all podcasts with metadata.

        When there are none, ``podcasts`` is empty and ``empty_reason`` says why:

        - ``"not_in_backup"``: no MTLibrary.sqlite.
        - ``"no_records"``: MTLibrary.sqlite exists but holds no podcasts.

        A MTLibrary.sqlite that is listed in the backup but can't be read is an
        ``error``, with no ``empty_reason``.
        """
        db_path = get_database(backup, self.PODCAST_DB_PATH,
                               domain="AppDomainGroup-243LU875E5.groups.com.apple.podcasts")
        if not db_path:
            if backup.list_files(domain="AppDomainGroup-243LU875E5.groups.com.apple.podcasts",
                                 path_like=self.PODCAST_DB_PATH):
                return {
                    "podcasts": [],
                    "error": (
                        "Podcast is in this backup but could not be read. "
                        "The backup may be incomplete or damaged."
                    ),
                }
            return {"podcasts": [], "empty_reason": "not_in_backup"}

        podcasts = []
        try:
            conn = connect_readonly(db_path, backup)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("PRAGMA table_info(ZMTPODCAST)")
            columns = [col["name"] for col in cursor.fetchall()]

            rows = cursor.execute(f"""
                SELECT
                    ROWID, ZTITLE, ZITEMDESCRIPTION, ZFEEDURL, ZWEBPAGEURL, ZSUBSCRIBED
                FROM ZMTPODCAST
            """).fetchall()

            for row in rows:
                podcasts.append({
                    "id": row["ROWID"],
                    "title": row["ZTITLE"],
                    "description": row["ZITEMDESCRIPTION"],
                    "feed_url": row["ZFEEDURL"],
                    "webpage_url": row["ZWEBPAGEURL"],
                    "subscribed": row["ZSUBSCRIBED"] == 1,
                })

            conn.close()
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"podcasts": [], "error": str(e)}

        if not podcasts:
            return {"podcasts": [], "empty_reason": "no_records"}
        return {"podcasts": podcasts}
