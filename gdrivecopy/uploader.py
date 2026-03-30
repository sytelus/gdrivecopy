"""Upload orchestration.

Implements the three-phase workflow described in the PRD:

1. **Scan Drive** -- build an in-memory map of what's already uploaded.
2. **Scan Local & Upload** -- walk the source directory, skip files already
   on Drive, upload the rest with concurrent workers.
3. **Report** -- print a summary.

Thread safety: upload workers run in a ``ThreadPoolExecutor``.  The shared
``DriveClient`` uses ``AuthorizedSession`` which handles token refresh with
its own lock.  The ``SessionCache`` is internally locked.  The ``UploadStats``
counters are updated only from the main thread after futures complete.
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from pathlib import Path

from gdrivecopy.drive import (
    DriveApiError,
    DriveClient,
    DailyLimitError,
    RateLimitError,
    UploadResponse,
)
from gdrivecopy.models import (
    DriveFile,
    LocalFile,
    SessionEntry,
    UploadConfig,
    UploadStats,
)
from gdrivecopy.scanner import scan_local
from gdrivecopy.session import SessionCache

logger = logging.getLogger(__name__)

# Files at or below this size use multipart upload (single request).
MULTIPART_THRESHOLD = 8 * 1024 * 1024  # 8 MiB
MAX_RETRIES = 8


class _CircuitBreaker:
    """Simple circuit breaker: pauses after N consecutive failures."""

    def __init__(self, threshold: int = 10, pause_seconds: int = 60) -> None:
        self._threshold = threshold
        self._pause = pause_seconds
        self._consecutive_failures = 0
        self._lock = threading.Lock()

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._threshold:
                logger.warning(
                    "Circuit breaker: %d consecutive failures, pausing %ds",
                    self._consecutive_failures,
                    self._pause,
                )
                self._consecutive_failures = 0
        # Sleep outside lock so other threads aren't blocked.
        time.sleep(self._pause)


class Uploader:
    """Orchestrates the full upload workflow.

    Args:
        config: Upload configuration (CLI arguments).
        drive: Authenticated Drive API client.
    """

    def __init__(self, config: UploadConfig, drive: DriveClient) -> None:
        self._config = config
        self._drive = drive
        self._session_cache = SessionCache(config.session_path)
        self._stats = UploadStats()
        self._breaker = _CircuitBreaker()
        self._daily_limit_hit = threading.Event()

        # Populated during Phase 1.
        self._file_map: dict[str, DriveFile] = {}
        self._folder_map: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> UploadStats:
        """Execute the full scan → upload → report workflow.

        Returns:
            Final upload statistics.
        """
        start = time.monotonic()

        # Phase 1: Scan Drive
        logger.info("Phase 1: Scanning Drive folder %s", self._config.drive_folder_id)
        self._file_map, self._folder_map = self._drive.list_all(
            self._config.drive_folder_id
        )

        # Load the session cache (if present).
        self._session_cache.load()

        # Phase 2: Scan local & upload
        logger.info("Phase 2: Scanning local directory %s", self._config.source_dir)
        files_to_upload: list[LocalFile] = []

        for lf in scan_local(self._config.source_dir):
            self._stats.files_scanned += 1
            action = self._classify(lf)

            if action == "skip":
                self._stats.files_skipped += 1
            elif action == "size_mismatch":
                self._stats.files_skipped += 1
                self._stats.size_mismatches += 1
            elif action == "upload":
                files_to_upload.append(lf)

        logger.info(
            "Scan complete: %d to upload, %d skipped, %d size mismatches",
            len(files_to_upload),
            self._stats.files_skipped,
            self._stats.size_mismatches,
        )

        if self._config.dry_run:
            logger.info("Dry run -- skipping uploads")
        elif files_to_upload:
            self._upload_all(files_to_upload)

        self._stats.duration_seconds = time.monotonic() - start
        return self._stats

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify(self, lf: LocalFile) -> str:
        """Decide whether to skip or upload a local file.

        Returns one of ``"skip"``, ``"size_mismatch"``, or ``"upload"``.
        """
        drive_file = self._file_map.get(lf.relative_path)
        if drive_file is None:
            return "upload"

        if drive_file.size == lf.size:
            logger.debug("Skipping (on Drive, same size): %s", lf.relative_path)
            return "skip"

        detail = (
            f"{lf.relative_path}: local={lf.size} bytes, "
            f"drive={drive_file.size} bytes"
        )
        logger.warning("Size mismatch: %s", detail)
        self._stats.mismatch_details.append(detail)
        return "size_mismatch"

    # ------------------------------------------------------------------
    # Concurrent upload
    # ------------------------------------------------------------------

    def _upload_all(self, files: list[LocalFile]) -> None:
        """Upload all files using a thread pool."""
        logger.info("Uploading %d files with %d workers", len(files), self._config.transfers)

        with ThreadPoolExecutor(max_workers=self._config.transfers) as pool:
            futures: dict[Future[bool], LocalFile] = {}
            for lf in files:
                if self._daily_limit_hit.is_set():
                    break
                fut = pool.submit(self._upload_one, lf)
                futures[fut] = lf

            for fut in as_completed(futures):
                lf = futures[fut]
                try:
                    success = fut.result()
                    if success:
                        self._breaker.record_success()
                except DailyLimitError:
                    self._stats.daily_limit_hits += 1
                    self._daily_limit_hit.set()
                    logger.error(
                        "Daily upload limit (750 GB) reached. "
                        "Re-run the tool after ~24 hours."
                    )
                    # Cancel pending futures.
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                except Exception as exc:
                    self._breaker.record_failure()
                    self._stats.files_failed += 1
                    self._stats.errors.append(f"{lf.relative_path}: {exc}")
                    logger.error("Failed: %s -- %s", lf.relative_path, exc)

    # ------------------------------------------------------------------
    # Single-file upload
    # ------------------------------------------------------------------

    def _upload_one(self, lf: LocalFile) -> bool:
        """Upload a single file with retries.  Returns True on success."""
        for attempt in range(MAX_RETRIES):
            if self._daily_limit_hit.is_set():
                raise DailyLimitError(403, "Daily limit reached")
            try:
                return self._upload_one_attempt(lf, attempt)
            except DailyLimitError:
                raise  # bubble up immediately
            except RateLimitError as exc:
                delay = min(60, 2 ** attempt) * random.random()
                logger.warning(
                    "Rate limited on %s (attempt %d/%d), backing off %.1fs: %s",
                    lf.relative_path, attempt + 1, MAX_RETRIES, delay, exc,
                )
                time.sleep(delay)
            except DriveApiError as exc:
                if exc.status < 500 and exc.status not in (429,):
                    # Permanent client error -- no point retrying.
                    self._stats.files_failed += 1
                    self._stats.errors.append(f"{lf.relative_path}: {exc}")
                    logger.error("Permanent failure: %s -- %s", lf.relative_path, exc)
                    return False
                delay = min(60, 2 ** attempt) * random.random()
                logger.warning(
                    "Transient error on %s (attempt %d/%d), retrying in %.1fs: %s",
                    lf.relative_path, attempt + 1, MAX_RETRIES, delay, exc,
                )
                time.sleep(delay)
            except OSError as exc:
                self._stats.files_failed += 1
                self._stats.errors.append(f"{lf.relative_path}: {exc}")
                logger.error("Local read error: %s -- %s", lf.relative_path, exc)
                return False

        # Exhausted retries.
        self._stats.files_failed += 1
        self._stats.errors.append(f"{lf.relative_path}: max retries exceeded")
        logger.error("Max retries exceeded: %s", lf.relative_path)
        return False

    def _upload_one_attempt(self, lf: LocalFile, attempt: int) -> bool:
        """Single upload attempt for one file.  Returns True on success."""
        parent_id = self._ensure_parent_folder(lf.relative_path)

        if lf.size <= MULTIPART_THRESHOLD:
            return self._upload_small(lf, parent_id)
        return self._upload_resumable(lf, parent_id, attempt)

    # ------------------------------------------------------------------
    # Small-file upload
    # ------------------------------------------------------------------

    def _upload_small(self, lf: LocalFile, parent_id: str) -> bool:
        """Upload a file ≤ 8 MiB with a single multipart request."""
        logger.info("Uploading (small): %s (%d bytes)", lf.relative_path, lf.size)

        result = self._drive.multipart_upload(
            file_path=lf.path,
            name=lf.path.name,
            parent_id=parent_id,
            created_time=lf.ctime,
            modified_time=lf.mtime,
        )

        if self._config.verify_checksum:
            local_md5 = hashlib.md5(lf.path.read_bytes()).hexdigest()
            if result.md5_checksum and local_md5 != result.md5_checksum:
                logger.error(
                    "MD5 mismatch for %s: local=%s drive=%s",
                    lf.relative_path, local_md5, result.md5_checksum,
                )
                self._drive.trash_file(result.file_id)
                raise DriveApiError(0, f"MD5 mismatch: {lf.relative_path}")

        self._stats.files_uploaded += 1
        self._stats.bytes_uploaded += lf.size
        self._session_cache.remove(lf.relative_path)
        logger.info("Uploaded: %s", lf.relative_path)
        return True

    # ------------------------------------------------------------------
    # Resumable upload
    # ------------------------------------------------------------------

    def _upload_resumable(
        self, lf: LocalFile, parent_id: str, attempt: int
    ) -> bool:
        """Upload a file > 8 MiB using the resumable upload protocol."""
        session_uri: str | None = None
        resume_offset = 0
        resumed = False

        # Try to resume from session cache.
        cached = self._session_cache.get(lf.relative_path)
        if cached is not None:
            if cached.file_size == lf.size and cached.mtime == lf.mtime:
                try:
                    confirmed = self._drive.query_upload_status(
                        cached.session_uri, lf.size
                    )
                    if confirmed == lf.size:
                        # Already completed (e.g. from another machine).
                        logger.info(
                            "Session already completed: %s", lf.relative_path
                        )
                        self._session_cache.remove(lf.relative_path)
                        self._stats.files_uploaded += 1
                        self._stats.bytes_uploaded += lf.size
                        self._stats.files_resumed += 1
                        return True
                    session_uri = cached.session_uri
                    resume_offset = confirmed
                    resumed = True
                    logger.info(
                        "Resuming %s from byte %d / %d",
                        lf.relative_path, resume_offset, lf.size,
                    )
                except DriveApiError:
                    logger.info(
                        "Stale session for %s (expired or invalid), starting fresh",
                        lf.relative_path,
                    )
            else:
                logger.warning(
                    "Discarding stale session for %s (file changed)",
                    lf.relative_path,
                )
            if session_uri is None:
                self._session_cache.remove(lf.relative_path)

        # Initiate a new session if needed.
        if session_uri is None:
            session_uri = self._drive.initiate_resumable_upload(
                name=lf.path.name,
                parent_id=parent_id,
                file_size=lf.size,
                created_time=lf.ctime,
                modified_time=lf.mtime,
            )
            self._session_cache.put(
                lf.relative_path,
                SessionEntry(
                    session_uri=session_uri,
                    file_size=lf.size,
                    mtime=lf.mtime,
                ),
            )

        # Stream chunks while computing MD5.
        hasher = hashlib.md5()
        chunk_size = self._config.chunk_size
        offset = 0
        result: UploadResponse | None = None

        with open(lf.path, "rb") as f:
            # Hash the already-uploaded portion (for MD5 continuity).
            if resume_offset > 0:
                remaining = resume_offset
                while remaining > 0:
                    block = f.read(min(chunk_size, remaining))
                    if not block:
                        break
                    hasher.update(block)
                    remaining -= len(block)
                offset = resume_offset

            # Upload remaining chunks.
            while offset < lf.size:
                data = f.read(chunk_size)
                if not data:
                    break
                hasher.update(data)

                result = self._drive.upload_chunk(
                    session_uri=session_uri,
                    data=data,
                    start=offset,
                    total=lf.size,
                )
                offset += len(data)

                # Bandwidth throttling.
                if self._config.bwlimit:
                    expected = len(data) / self._config.bwlimit
                    time.sleep(max(0, expected - 0.001))

                if not self._config.quiet and offset < lf.size:
                    pct = offset * 100 / lf.size
                    logger.debug(
                        "%s: %.1f%% (%d / %d bytes)",
                        lf.relative_path, pct, offset, lf.size,
                    )

        if result is None:
            raise DriveApiError(0, f"Upload produced no completion response: {lf.relative_path}")

        # Verify MD5.
        local_md5 = hasher.hexdigest()
        if self._config.verify_checksum and result.md5_checksum:
            if local_md5 != result.md5_checksum:
                logger.error(
                    "MD5 mismatch for %s: local=%s drive=%s",
                    lf.relative_path, local_md5, result.md5_checksum,
                )
                self._drive.trash_file(result.file_id)
                raise DriveApiError(0, f"MD5 mismatch: {lf.relative_path}")

        # Success.
        self._session_cache.remove(lf.relative_path)
        self._stats.files_uploaded += 1
        self._stats.bytes_uploaded += lf.size
        if resumed:
            self._stats.files_resumed += 1
        logger.info("Uploaded: %s (%s)", lf.relative_path, local_md5)
        return True

    # ------------------------------------------------------------------
    # Folder management
    # ------------------------------------------------------------------

    def _ensure_parent_folder(self, relative_path: str) -> str:
        """Create parent folders on Drive if they don't exist.

        Returns the Drive folder ID for the immediate parent of *relative_path*.
        """
        parts = relative_path.split("/")[:-1]  # drop filename
        if not parts:
            return self._config.drive_folder_id

        current_path = ""
        parent_id = self._config.drive_folder_id

        for part in parts:
            current_path = f"{current_path}{part}/" if current_path else f"{part}/"
            if current_path in self._folder_map:
                parent_id = self._folder_map[current_path]
            else:
                parent_id = self._drive.create_folder(part, parent_id)
                self._folder_map[current_path] = parent_id
                logger.info("Created folder: %s", current_path)

        return parent_id
