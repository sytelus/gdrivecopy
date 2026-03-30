"""Session cache for byte-level upload resume.

Manages ``sessions.json`` -- a disposable JSON file that maps in-progress
uploads to their Google Drive resumable session URIs.  This is an optimisation,
not state: the tool works correctly without it (files not on Drive are simply
uploaded from scratch).

The cache is thread-safe: a ``threading.Lock`` serialises reads and writes so
concurrent upload workers can share a single ``SessionCache`` instance.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from gdrivecopy.models import SessionEntry

logger = logging.getLogger(__name__)


class SessionCache:
    """Thread-safe read/write access to the session cache file.

    Usage::

        cache = SessionCache(Path("sessions.json"))
        cache.load()

        entry = cache.get("photos/img001.jpg")
        if entry:
            ...  # attempt resume

        cache.put("photos/img001.jpg", SessionEntry(...))
        cache.save()
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, SessionEntry] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load the cache from disk.  Missing or corrupt files are ignored."""
        with self._lock:
            if not self._path.exists():
                logger.info("No session cache found at %s", self._path)
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._data = {
                    key: SessionEntry(
                        session_uri=val["session_uri"],
                        file_size=val["file_size"],
                        mtime=val["mtime"],
                    )
                    for key, val in raw.items()
                }
                logger.info(
                    "Loaded %d session(s) from %s", len(self._data), self._path
                )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning("Ignoring corrupt session cache: %s", exc)
                self._data = {}

    def save(self) -> None:
        """Write the current cache to disk atomically."""
        with self._lock:
            payload = {
                key: {
                    "session_uri": entry.session_uri,
                    "file_size": entry.file_size,
                    "mtime": entry.mtime,
                }
                for key, entry in self._data.items()
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    # ------------------------------------------------------------------
    # Entry access
    # ------------------------------------------------------------------

    def get(self, relative_path: str) -> SessionEntry | None:
        """Return the cached session for *relative_path*, or ``None``."""
        with self._lock:
            return self._data.get(relative_path)

    def put(self, relative_path: str, entry: SessionEntry) -> None:
        """Insert or update a session entry and persist to disk."""
        with self._lock:
            self._data[relative_path] = entry
        self.save()

    def remove(self, relative_path: str) -> None:
        """Remove a session entry (if present) and persist to disk."""
        with self._lock:
            self._data.pop(relative_path, None)
        self.save()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
