"""Local filesystem scanner.

Walks the source directory and yields ``LocalFile`` objects for every regular
file.  Symlinks are skipped with a warning.  The caller uses these objects to
decide whether to upload each file.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from gdrivecopy.models import LocalFile

logger = logging.getLogger(__name__)


def _iso_from_timestamp(ts: float) -> str:
    """Convert a POSIX timestamp to an ISO 8601 UTC string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def scan_local(source_dir: Path) -> Iterator[LocalFile]:
    """Walk *source_dir* recursively and yield a ``LocalFile`` for each file.

    - Symlinks are skipped (logged at WARNING level).
    - Directories are traversed but not yielded.
    - The ``relative_path`` always uses ``/`` as the separator so that it can
      be compared directly with Google Drive paths regardless of OS.

    Args:
        source_dir: Root directory to scan.  Must exist.

    Yields:
        ``LocalFile`` instances in filesystem walk order.
    """
    source_dir = source_dir.resolve()

    for dirpath, dirnames, filenames in os.walk(source_dir):
        # Sort for deterministic order across runs.
        dirnames.sort()
        filenames.sort()

        for fname in filenames:
            full_path = Path(dirpath) / fname

            if full_path.is_symlink():
                rel = full_path.relative_to(source_dir).as_posix()
                logger.warning("Skipping symlink: %s", rel)
                continue

            try:
                stat = full_path.stat()
            except OSError as exc:
                rel = full_path.relative_to(source_dir).as_posix()
                logger.error("Cannot stat %s: %s", rel, exc)
                continue

            yield LocalFile(
                path=full_path,
                relative_path=full_path.relative_to(source_dir).as_posix(),
                size=stat.st_size,
                mtime=_iso_from_timestamp(stat.st_mtime),
                # On Windows st_ctime is creation time; on Linux it is inode
                # change time (best-effort).
                ctime=_iso_from_timestamp(stat.st_ctime),
            )
