"""SQLite persistence. All SQL is parameterised.

Schema changes are applied in place and idempotently, so a database created by an
earlier version opens cleanly with this one.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    code        TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    created_at  REAL NOT NULL,
    click_count INTEGER NOT NULL DEFAULT 0,
    expires_at  REAL
);
CREATE TABLE IF NOT EXISTS clicks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL REFERENCES links(code),
    clicked_at REAL NOT NULL,
    referrer   TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks(code);
"""


@dataclass(frozen=True)
class Link:
    code: str
    url: str
    created_at: float
    click_count: int
    expires_at: float | None = None


class DuplicateCode(Exception):
    """The short code is already taken."""


class Storage:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Bring a database created by an earlier version up to schema version 2."""
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(links)")}
        if "expires_at" not in cols:  # v1 database: SQLite has no ADD COLUMN IF NOT EXISTS
            self._conn.execute("ALTER TABLE links ADD COLUMN expires_at REAL")
        # PRAGMA cannot take bound parameters, so the version is a literal rather than string-built SQL.
        self._conn.execute("PRAGMA user_version = 2")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_link(row: sqlite3.Row) -> Link:
        return Link(row["code"], row["url"], row["created_at"], row["click_count"], row["expires_at"])

    def insert_link(
        self,
        code: str,
        url: str,
        created_at: float | None = None,
        expires_at: float | None = None,
    ) -> Link:
        created = time.time() if created_at is None else created_at
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO links (code, url, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (code, url, created, expires_at),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateCode(code) from exc
        return Link(code, url, created, 0, expires_at)

    def get_link(self, code: str) -> Link | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT code, url, created_at, click_count, expires_at FROM links WHERE code = ?",
                (code,),
            ).fetchone()
        return self._row_to_link(row) if row else None

    def record_click(
        self,
        code: str,
        referrer: str | None = None,
        user_agent: str | None = None,
        at: float | None = None,
    ) -> None:
        when = time.time() if at is None else at
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "INSERT INTO clicks (code, clicked_at, referrer, user_agent) VALUES (?, ?, ?, ?)",
                    (code, when, (referrer or None) and referrer[:255], (user_agent or None) and user_agent[:255]),
                )
                self._conn.execute(
                    "UPDATE links SET click_count = click_count + 1 WHERE code = ?", (code,)
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def click_stats(self, code: str, top_n: int = 5) -> dict:
        with self._lock:
            last = self._conn.execute(
                "SELECT MAX(clicked_at) AS last FROM clicks WHERE code = ?", (code,)
            ).fetchone()
            tops = self._conn.execute(
                "SELECT referrer, COUNT(*) AS n FROM clicks "
                "WHERE code = ? AND referrer IS NOT NULL "
                "GROUP BY referrer ORDER BY n DESC, referrer ASC LIMIT ?",
                (code, top_n),
            ).fetchall()
        return {
            "last_clicked_at": last["last"],
            "top_referrers": [{"referrer": r["referrer"], "clicks": r["n"]} for r in tops],
        }
