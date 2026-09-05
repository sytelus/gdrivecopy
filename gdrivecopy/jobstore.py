"""Crash-safe, disk-backed jobs, manifests, checkpoints, and audit events.

SQLite WAL keeps updates small at millions of files. FULL synchronization is
intentional: losing a create ID after sending content can create duplicates.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from gdrivecopy.models import SessionEntry
from gdrivecopy.session import SessionCache


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobLock:
    """OS-owned lock: released automatically on exit, crash, or reboot."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = self.path.open("a+b")
        try:
            self.stream.seek(0)
            if self.stream.read(1) == b"":
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise RuntimeError(
                "This job or account registry is already open in another process"
            ) from exc
        return self

    def __exit__(self, *_args):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


class JobStore:
    """One database per immutable source/destination/account job."""

    def __init__(self, directory: Path, *, readonly: bool = False) -> None:
        if not readonly:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self._lock = threading.RLock()
        path = directory / "job.sqlite3"
        self.db = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro" if readonly else path,
            uri=readonly,
            check_same_thread=False,
            timeout=30,
        )
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            self.db.close()
            raise ValueError(
                f"Unsupported job schema {version}; use a compatible gdrivecopy version"
            )
        self.db.row_factory = sqlite3.Row
        if readonly:
            return
        if os.name != "nt":
            (directory / "job.sqlite3").chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS remote(
                path TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, parent TEXT,
                folder INTEGER NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS remote_parent ON remote(parent,path);
            CREATE TABLE IF NOT EXISTS folders(
                id TEXT PRIMARY KEY, path TEXT NOT NULL, token TEXT,
                status TEXT NOT NULL DEFAULT 'pending', generation INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS folders_pending ON folders(status,path);
            CREATE INDEX IF NOT EXISTS folders_path ON folders(path);
            CREATE TABLE IF NOT EXISTS folder_seen(
                parent TEXT, id TEXT, PRIMARY KEY(parent,id)
            );
            CREATE TABLE IF NOT EXISTS page_tokens(parent TEXT, token TEXT, PRIMARY KEY(parent,token));
            CREATE TABLE IF NOT EXISTS files(
                path TEXT PRIMARY KEY, data TEXT NOT NULL, size INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', remote_id TEXT,
                session TEXT, offset INTEGER NOT NULL DEFAULT 0,
                proof TEXT, error TEXT, updated TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS files_status ON files(status,path);
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
                kind TEXT NOT NULL, path TEXT NOT NULL, data TEXT NOT NULL
            );
        """)
        self.db.commit()
        self.db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        with self._lock, self.db:
            yield self.db

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def get(self, key: str, default=None):
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    def event(self, kind: str, path: str = "", **data) -> None:
        # Session URIs, OAuth tokens and headers never enter audit payloads.
        with self.transaction() as db:
            db.execute(
                "INSERT INTO events(time,kind,path,data) VALUES(?,?,?,?)",
                (utc_now(), kind, path, json.dumps(data)),
            )

    def rows(self, query: str, args=(), batch_size: int = 500) -> Iterator[dict]:
        """Stream bounded batches without holding a DB lock across network I/O."""
        with self._lock:
            cursor = self.db.execute(query, args)
        try:
            while True:
                with self._lock:
                    rows = cursor.fetchmany(batch_size)
                if not rows:
                    return
                yield from (dict(row) for row in rows)
        finally:
            with self._lock:
                cursor.close()

    def file(self, path: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        return dict(row) if row else None

    def add_files(self, items: list[tuple[str, dict, int]]) -> None:
        with self.transaction() as db:
            db.executemany(
                "INSERT OR IGNORE INTO files(path,data,size,updated) VALUES(?,?,?,?)",
                [(path, json.dumps(data), size, utc_now()) for path, data, size in items],
            )

    def update_file(self, path: str, **values) -> None:
        allowed = {"status", "remote_id", "session", "offset", "proof", "error", "data", "size"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Invalid checkpoint fields")
        assignments = ",".join(f"{key}=?" for key in values)
        with self.transaction() as db:
            db.execute(
                f"UPDATE files SET {assignments},updated=? WHERE path=?",
                (*values.values(), utc_now(), path),
            )

    def counts(self) -> dict[str, dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT status,COUNT(*) AS files,COALESCE(SUM(size),0) AS bytes "
                "FROM files GROUP BY status"
            ).fetchall()
        return {row["status"]: {"files": row["files"], "bytes": row["bytes"]} for row in rows}

    def reset_incomplete(self) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE files SET status='pending',error=NULL WHERE status IN "
                "('running','failed','cancelled','skipped_size','skipped_verified') "
                "OR (status='conflict' AND error NOT LIKE 'Unsafe namespace:%')"
            )

    def export_audit(self, destination: Path) -> None:
        with destination.open("x", encoding="utf-8") as stream:
            for row in self.rows("SELECT * FROM events ORDER BY seq"):
                row["data"] = json.loads(row["data"])
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")


class DatabaseSessions:
    """Uploader session adapter; durable updates touch only one file row."""

    def __init__(self, store: JobStore) -> None:
        self.store = store

    def get(self, path: str) -> SessionEntry | None:
        row = self.store.file(path)
        if not row or not row["session"]:
            return None
        return SessionCache._parse_entry(path, json.loads(row["session"]))

    def put(self, path: str, entry: SessionEntry) -> None:
        from dataclasses import asdict

        self.store.update_file(path, session=json.dumps(asdict(entry)))

    def remove(self, path: str) -> None:
        self.store.update_file(path, session=None)

    def load(self) -> None:
        pass  # SQLite is already open; do not load a full manifest into RAM.
