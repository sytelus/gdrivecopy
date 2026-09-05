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
from urllib.parse import urlsplit

from gdrivecopy.models import SessionEntry
from gdrivecopy.persistence import write_text_atomic

logger = logging.getLogger(__name__)


def validate_session_uri(uri: str) -> None:
    """Reject destinations that must never receive OAuth headers or file bytes.

    The only supported upload endpoint is Drive v3 on www.googleapis.com.
    Do not include the URI in errors: its query contains a session capability.
    """
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.googleapis.com"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path != "/upload/drive/v3/files"
        or any(char.isspace() for char in uri)
    ):
        raise ValueError("Invalid or untrusted Drive upload session URI")


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
        self._lock = threading.RLock()  # reentrant so put/remove can call save

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load the cache from disk.  Missing or corrupt files are ignored."""
        with self._lock:
            if not self._path.exists():
                logger.info("No session cache found at %s", self._path)
                self._data = {}
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise TypeError("top-level value must be an object")
                self._data = {key: self._parse_entry(key, val) for key, val in raw.items()}
                logger.info("Loaded %d session(s) from %s", len(self._data), self._path)
            except (
                json.JSONDecodeError,
                KeyError,
                OSError,
                TypeError,
                UnicodeError,
                ValueError,
            ) as exc:
                logger.warning("Ignoring corrupt session cache: %s", exc)
                self._data = {}

    @staticmethod
    def _parse_entry(key: object, value: object) -> SessionEntry:
        """Validate and convert one untrusted JSON cache entry."""
        if not isinstance(key, str) or not isinstance(value, dict):
            raise TypeError("session entries must map string paths to objects")

        session_uri = value["session_uri"]
        file_size = value["file_size"]
        mtime = value["mtime"]
        if not isinstance(session_uri, str):
            raise ValueError(f"invalid session URI for {key!r}")
        validate_session_uri(session_uri)
        if not isinstance(file_size, int) or isinstance(file_size, bool) or file_size < 0:
            raise ValueError(f"invalid file size for {key!r}")
        if not isinstance(mtime, str) or not mtime:
            raise ValueError(f"invalid modification time for {key!r}")
        source_path = value.get("source_path")
        parent_id = value.get("parent_id")
        mtime_ns = value.get("mtime_ns")
        for field_value in (source_path, parent_id):
            if field_value is not None and (not isinstance(field_value, str) or not field_value):
                raise ValueError(f"invalid session identity for {key!r}")
        if mtime_ns is not None and (not isinstance(mtime_ns, int) or isinstance(mtime_ns, bool)):
            raise ValueError(f"invalid nanosecond modification time for {key!r}")
        return SessionEntry(session_uri, file_size, mtime, source_path, parent_id, mtime_ns)

    def save(self) -> None:
        """Write the current cache to disk atomically."""
        with self._lock:
            payload = {
                key: {
                    "session_uri": entry.session_uri,
                    "file_size": entry.file_size,
                    "mtime": entry.mtime,
                    "source_path": entry.source_path,
                    "parent_id": entry.parent_id,
                    "mtime_ns": entry.mtime_ns,
                }
                for key, entry in self._data.items()
            }
            write_text_atomic(self._path, json.dumps(payload, indent=2))

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
            self._persist()

    def remove(self, relative_path: str) -> None:
        """Remove a session entry (if present) and persist to disk."""
        with self._lock:
            if relative_path in self._data:
                del self._data[relative_path]
                self._persist()

    def _persist(self) -> None:
        """Keep live session state usable even if its optional disk cache fails.

        In particular, disk failure must never bypass checksum verification or
        cleanup after Drive has accepted a completed upload.
        """
        try:
            self.save()
        except OSError as exc:
            logger.warning(
                "Cannot persist session cache; restart resume may be unavailable: %s", exc
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
