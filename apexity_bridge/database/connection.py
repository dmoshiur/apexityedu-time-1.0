"""SQLite access layer.

* One connection per thread (sqlite3 connections are not thread-safe).
* WAL journal mode → readers never block the writer.
* ``busy_timeout`` → concurrent writers wait instead of erroring.
* Foreign keys enforced.
* ``Database.transaction()`` → atomic BEGIN IMMEDIATE / COMMIT / ROLLBACK.
* Integrity check + backup helpers here so corruption handling has one home.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("apexity_bridge.database")


class DatabaseCorruptError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()   # serialises write transactions

    # -- connections --------------------------------------------------------
    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path) if str(self.path) != ":memory:" else "file:bridge-mem-%s?mode=memory&cache=shared" % id(self),
                uri=str(self.path) == ":memory:",
                timeout=10.0,
                detect_types=0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return conn

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        cur = self._connection().execute(sql, params)
        return cur.fetchall()

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        """Single-statement write (auto-committed). Returns lastrowid."""
        with self._write_lock:
            conn = self._connection()
            with conn:
                cur = conn.execute(sql, params)
            return cur.lastrowid or 0

    @contextmanager
    def transaction(self):
        """Atomic write transaction usable from any thread."""
        with self._write_lock:
            conn = self._connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # -- maintenance --------------------------------------------------------
    def check_integrity(self) -> bool:
        try:
            row = self.query_one("PRAGMA integrity_check(1)")
            ok = bool(row and row[0].lower() == "ok")
        except sqlite3.DatabaseError as exc:
            log.error("integrity_check failed: %s", exc)
            return False
        if not ok:
            log.error("SQLite integrity_check reported problems: %s", row[0] if row else "unknown")
        return ok

    def backup_to(self, target: Path | str) -> Path:
        """Consistent online backup using SQLite's backup API."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(target))
        try:
            with dest:
                self._connection().backup(dest)
        finally:
            dest.close()
        log.info("Database backed up to %s", target)
        return target

    def close_all(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @staticmethod
    def quarantine_corrupt(path: Path | str) -> Path:
        """Move a corrupt DB aside (never delete data automatically)."""
        path = Path(path)
        ts = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
        quarantined = path.with_name(f"{path.name}.corrupt-{ts}")
        shutil.move(str(path), str(quarantined))
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.exists():
                shutil.move(str(side), str(side) + f".corrupt-{ts}")
        log.critical(
            "Database file %s failed integrity checks and was moved to %s. "
            "A fresh database will be created; restore from backups and read "
            "docs/TROUBLESHOOTING.md before deleting anything.",
            path, quarantined,
        )
        return quarantined
