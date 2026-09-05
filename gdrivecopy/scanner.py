"""Local filesystem scanner.

Walks the source directory and yields ``LocalFile`` objects for every regular
file.  Symlinks are skipped with a warning.  The caller uses these objects to
decide whether to upload each file.
"""

from __future__ import annotations

import logging
import os
import stat as stat_module
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gdrivecopy.models import LocalFile

logger = logging.getLogger(__name__)


def _iso_from_timestamp(ts: float) -> str:
    """Convert a POSIX timestamp to an ISO 8601 UTC string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _creation_time(stat_result: os.stat_result) -> str | None:
    """Return a real creation time where the platform exposes one."""
    birth_time = getattr(stat_result, "st_birthtime", None)
    if birth_time is not None:
        return _iso_from_timestamp(birth_time)
    if os.name == "nt":
        return _iso_from_timestamp(stat_result.st_ctime)
    return None


def _is_link_like(path: Path) -> bool:
    """Return whether *path* is a symlink or Windows directory junction."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction and is_junction():
        return True
    if os.name == "nt":
        # Cloud placeholders are also reparse points, but are regular source
        # files. Only name-surrogate tags represent links to another path.
        tag = getattr(path.lstat(), "st_reparse_tag", 0)
        return bool(tag & 0x20000000)  # Windows IsReparseTagNameSurrogate bit.
    return False


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Result of scanning a local directory."""

    files: list[LocalFile]
    symlinks_skipped: int
    errors: list[str]
    files_excluded: int = 0


def scan_local(source_dir: Path, excluded_paths: Collection[Path] = ()) -> ScanResult:
    """Walk *source_dir* recursively and return all regular files.

    - Symlinks are skipped (logged at WARNING level) and counted.
    - Directory symlinks are skipped rather than followed.
    - Files and directories that cannot be inspected are recorded in
      ``ScanResult.errors`` so a run cannot silently appear complete.
    - Directories are traversed but not included.
    - The ``relative_path`` always uses ``/`` as the separator so that it can
      be compared directly with Google Drive paths regardless of OS.

    Args:
        source_dir: Root directory to scan.  Must exist.
        excluded_paths: Exact file paths to omit, normally gdrivecopy's own
            credentials, token, session, log, and report artifacts.

    Returns:
        A ``ScanResult`` with the file list, symlink count, and scan errors.

    Raises:
        NotADirectoryError: If *source_dir* is not an existing directory.
    """
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise NotADirectoryError(f"Source directory does not exist: {source_dir}")

    files: list[LocalFile] = []
    symlinks_skipped = 0
    files_excluded = 0
    errors: list[str] = []
    excluded = {path.resolve() for path in excluded_paths}
    temp_prefixes: dict[Path, tuple[str, ...]] = {}
    for parent in {path.parent for path in excluded}:
        temp_prefixes[parent] = tuple(
            f".{path.name}." for path in excluded if path.parent == parent
        )

    def _record_walk_error(exc: OSError) -> None:
        path = Path(exc.filename) if exc.filename else source_dir
        try:
            display_path = path.relative_to(source_dir).as_posix() or "."
        except ValueError:
            display_path = str(path)
        message = f"Cannot scan {display_path}: {exc}"
        logger.error(message)
        errors.append(message)

    for dirpath, dirnames, filenames in os.walk(
        source_dir, followlinks=False, onerror=_record_walk_error
    ):
        dirnames.sort()
        filenames.sort()

        # ``os.walk(..., followlinks=False)`` does not descend into directory
        # symlinks, but it still includes them in ``dirnames``.  Remove and
        # count them explicitly so the report accounts for every skipped link.
        for dirname in list(dirnames):
            full_path = Path(dirpath) / dirname
            try:
                link_like = _is_link_like(full_path)
            except OSError as exc:
                rel = full_path.relative_to(source_dir).as_posix()
                message = f"Cannot inspect {rel}: {exc}"
                logger.error(message)
                errors.append(message)
                dirnames.remove(dirname)
                continue
            if link_like:
                rel = full_path.relative_to(source_dir).as_posix()
                logger.warning("Skipping symlink: %s", rel)
                symlinks_skipped += 1
                dirnames.remove(dirname)

        for fname in filenames:
            full_path = Path(dirpath) / fname

            if full_path in excluded or (
                fname.endswith(".tmp") and fname.startswith(temp_prefixes.get(full_path.parent, ()))
            ):
                logger.debug(
                    "Excluding gdrivecopy-owned file: %s",
                    full_path.relative_to(source_dir).as_posix(),
                )
                files_excluded += 1
                continue

            try:
                if _is_link_like(full_path):
                    rel = full_path.relative_to(source_dir).as_posix()
                    logger.warning("Skipping symlink: %s", rel)
                    symlinks_skipped += 1
                    continue
                stat = full_path.stat()
                if not stat_module.S_ISREG(stat.st_mode):
                    rel = full_path.relative_to(source_dir).as_posix()
                    message = f"Skipping non-regular file: {rel}"
                    logger.error(message)
                    errors.append(message)
                    continue
                mtime = _iso_from_timestamp(stat.st_mtime)
                ctime = _creation_time(stat)
            except (OSError, OverflowError, ValueError) as exc:
                rel = full_path.relative_to(source_dir).as_posix()
                message = f"Cannot inspect {rel}: {exc}"
                logger.error(message)
                errors.append(message)
                continue

            files.append(
                LocalFile(
                    path=full_path,
                    relative_path=full_path.relative_to(source_dir).as_posix(),
                    size=stat.st_size,
                    mtime=mtime,
                    ctime=ctime,
                    mtime_ns=stat.st_mtime_ns,
                    device=stat.st_dev,
                    inode=stat.st_ino,
                )
            )

    return ScanResult(
        files=files,
        symlinks_skipped=symlinks_skipped,
        errors=errors,
        files_excluded=files_excluded,
    )
