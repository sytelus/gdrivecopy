"""Local filesystem scanner.

Walks the source directory and yields ``LocalFile`` objects for every regular
file.  Symlinks are skipped with a warning.  The caller uses these objects to
decide whether to upload each file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gdrivecopy.models import LocalFile

logger = logging.getLogger(__name__)


def _iso_from_timestamp(ts: float) -> str:
    """Convert a POSIX timestamp to an ISO 8601 UTC string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class ScanResult:
    """Result of scanning a local directory."""

    files: list[LocalFile]
    symlinks_skipped: int


def scan_local(source_dir: Path) -> ScanResult:
    """Walk *source_dir* recursively and return all regular files.

    - Symlinks are skipped (logged at WARNING level) and counted.
    - Directories are traversed but not included.
    - The ``relative_path`` always uses ``/`` as the separator so that it can
      be compared directly with Google Drive paths regardless of OS.

    Args:
        source_dir: Root directory to scan.  Must exist.

    Returns:
        A ``ScanResult`` with the file list and symlink count.
    """
    source_dir = source_dir.resolve()
    files: list[LocalFile] = []
    symlinks_skipped = 0

    for dirpath, dirnames, filenames in os.walk(source_dir):
        dirnames.sort()
        filenames.sort()

        for fname in filenames:
            full_path = Path(dirpath) / fname

            if full_path.is_symlink():
                rel = full_path.relative_to(source_dir).as_posix()
                logger.warning("Skipping symlink: %s", rel)
                symlinks_skipped += 1
                continue

            try:
                stat = full_path.stat()
            except OSError as exc:
                rel = full_path.relative_to(source_dir).as_posix()
                logger.error("Cannot stat %s: %s", rel, exc)
                continue

            files.append(LocalFile(
                path=full_path,
                relative_path=full_path.relative_to(source_dir).as_posix(),
                size=stat.st_size,
                mtime=_iso_from_timestamp(stat.st_mtime),
                ctime=_iso_from_timestamp(stat.st_ctime),
            ))

    return ScanResult(files=files, symlinks_skipped=symlinks_skipped)
