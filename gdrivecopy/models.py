"""Shared data types used across all gdrivecopy modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DriveFile:
    """A file as it exists on Google Drive."""

    id: str
    name: str
    size: int
    md5_checksum: str | None = None


@dataclass
class LocalFile:
    """A file on the local filesystem.

    Attributes:
        path: Absolute path to the file.
        relative_path: Path relative to the source root, using ``/`` as separator
            regardless of OS so it can be compared with Drive paths.
        size: File size in bytes.
        mtime: Last-modified time as ISO 8601 string.
        ctime: Creation time as ISO 8601 string.  On Windows ``st_ctime`` is the
            real creation time; on Linux it is the inode change time (best-effort).
    """

    path: Path
    relative_path: str
    size: int
    mtime: str
    ctime: str | None = None


@dataclass
class SessionEntry:
    """An in-progress upload session stored in ``sessions.json``.

    Stores enough information to decide whether a cached session is still valid:
    the session URI plus the file's size and mtime *at the time the upload started*.
    If either has changed, the session is stale and must be discarded.
    """

    session_uri: str
    file_size: int
    mtime: str


@dataclass
class UploadConfig:
    """Configuration for a single upload run, built from CLI arguments."""

    source_dir: Path
    drive_folder_id: str
    transfers: int = 4
    chunk_size: int = 64 * 1024 * 1024  # 64 MiB
    bwlimit: int | None = None  # bytes per second, None = unlimited
    dry_run: bool = False
    credentials_path: Path = field(default_factory=lambda: Path("credentials.json"))
    token_path: Path = field(default_factory=lambda: Path("token.json"))
    session_path: Path = field(default_factory=lambda: Path("sessions.json"))
    verify_checksum: bool = True
    wait_on_limit: bool = False
    quiet: bool = False
    log_dir: Path = field(default_factory=lambda: Path("."))
    log_level: str = "INFO"


@dataclass
class UploadStats:
    """Mutable counters collected during an upload run.

    All fields are plain integers/lists so they can be updated safely from the
    main thread (the ``Uploader`` serialises result handling).
    """

    files_scanned: int = 0
    files_uploaded: int = 0
    bytes_uploaded: int = 0
    files_resumed: int = 0
    files_skipped: int = 0
    size_mismatches: int = 0
    files_failed: int = 0
    duration_seconds: float = 0.0
    daily_limit_hits: int = 0
    errors: list[str] = field(default_factory=list)
    mismatch_details: list[str] = field(default_factory=list)
