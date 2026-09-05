"""Shared data types used across all gdrivecopy modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DriveFile:
    """A file as it exists on Google Drive."""

    id: str
    name: str
    size: int | None
    md5_checksum: str | None = None


@dataclass(frozen=True, slots=True)
class LocalFile:
    """A file on the local filesystem.

    Attributes:
        path: Absolute path to the file.
        relative_path: Path relative to the source root, using ``/`` as separator
            regardless of OS so it can be compared with Drive paths.
        size: File size in bytes.
        mtime: Last-modified time as ISO 8601 string.
        mtime_ns: Last-modified time in nanoseconds, used to detect changes that
            occur between scanning and uploading without losing sub-microsecond
            filesystem precision.  ``None`` is accepted for callers that only
            have the portable ISO timestamp.
        ctime: Creation time as an ISO 8601 string on Windows and platforms
            that expose ``st_birthtime``.  ``None`` where creation time is not
            available rather than substituting Linux inode-change time.
        device: Optional filesystem device ID captured by the scanner.
        inode: Optional file identity paired with device to detect replacements
            that preserve the scanned size and modification time.
    """

    path: Path
    relative_path: str
    size: int
    mtime: str
    ctime: str | None = None
    mtime_ns: int | None = None
    device: int | None = None
    inode: int | None = None


@dataclass(frozen=True, slots=True)
class SessionEntry:
    """An in-progress upload session stored in ``sessions.json``.

    The session targets a specific absolute source path and actual Drive parent
    ID, with size and precise mtime captured at initiation. Legacy entries lack
    these optional identity fields and are discarded by the uploader.
    """

    session_uri: str
    file_size: int
    mtime: str
    source_path: str | None = None
    parent_id: str | None = None
    mtime_ns: int | None = None


@dataclass(slots=True)
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
    quiet: bool = False
    log_dir: Path = field(default_factory=lambda: Path("."))
    log_path: Path | None = None
    log_level: str = "INFO"


@dataclass(slots=True)
class UploadStats:
    """Mutable counters collected during an upload run.

    Updated only from the main thread in ``Uploader._upload_all`` after
    each worker future completes.
    """

    files_scanned: int = 0
    files_excluded: int = 0
    scan_errors: int = 0
    symlinks_skipped: int = 0
    files_to_upload: int = 0
    files_uploaded: int = 0
    bytes_uploaded: int = 0
    files_resumed: int = 0
    files_skipped: int = 0
    size_mismatches: int = 0
    path_conflicts: int = 0
    files_failed: int = 0
    duration_seconds: float = 0.0
    quota_limit_hits: int = 0
    errors: list[str] = field(default_factory=list)
    mismatch_details: list[str] = field(default_factory=list)
