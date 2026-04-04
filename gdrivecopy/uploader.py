"""Upload orchestration.

Implements the three-phase workflow described in the PRD:

1. **Scan Drive** -- build an in-memory map of what's already uploaded.
2. **Scan Local & Upload** -- walk the source directory, skip files already
   on Drive, upload the rest with concurrent workers.
3. **Report** -- print a summary.

Thread safety
-------------
Upload workers run in a ``ThreadPoolExecutor``.  Workers return a result
dataclass; **all** ``UploadStats`` mutations happen in the main thread after
a future completes.  The ``SessionCache`` is internally locked.  The
``_folder_map`` is guarded by ``_folder_lock`` to prevent duplicate folder
creation.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import itertools
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass

import requests.exceptions

from google.auth.transport.requests import Request
from googleapiclient.errors import HttpError

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
from gdrivecopy.scanner import scan_local, ScanResult
from gdrivecopy.session import SessionCache

logger = logging.getLogger(__name__)

MULTIPART_THRESHOLD = 8 * 1024 * 1024  # 8 MiB
MAX_RETRIES = 8


class ChecksumError(Exception):
    """Raised when the post-upload MD5 does not match the local file."""


# ------------------------------------------------------------------
# Worker result (returned by _upload_one, consumed by main thread)
# ------------------------------------------------------------------

@dataclass
class _WorkerResult:
    """Immutable result returned by a worker thread."""

    success: bool
    bytes_uploaded: int = 0
    resumed: bool = False
    error: str | None = None
    is_permanent: bool = False


# ------------------------------------------------------------------
# Circuit breaker
# ------------------------------------------------------------------

class _CircuitBreaker:
    """Pauses uploads after *threshold* consecutive failures."""

    def __init__(self, threshold: int = 10, pause_seconds: int = 60) -> None:
        self._threshold = threshold
        self._pause = pause_seconds
        self._consecutive_failures = 0
        self._lock = threading.Lock()

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> None:
        should_pause = False
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._threshold:
                logger.warning(
                    "Circuit breaker: %d consecutive failures, pausing %ds",
                    self._consecutive_failures,
                    self._pause,
                )
                self._consecutive_failures = 0
                should_pause = True
        if should_pause:
            time.sleep(self._pause)


# ------------------------------------------------------------------
# Uploader
# ------------------------------------------------------------------

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

        # Populated during Phase 1, guarded by _folder_lock for writes.
        self._file_map: dict[str, DriveFile] = {}
        self._folder_map: dict[str, str] = {}
        self._folder_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> UploadStats:
        """Execute the full scan -> upload -> report workflow."""
        start = time.monotonic()

        # Phase 1: Scan Drive.
        logger.info("Phase 1: Scanning Drive folder %s", self._config.drive_folder_id)
        self._file_map, self._folder_map = self._drive.list_all(
            self._config.drive_folder_id
        )
        self._session_cache.load()

        # Phase 2: Scan local & decide.
        logger.info("Phase 2: Scanning local directory %s", self._config.source_dir)
        scan = scan_local(self._config.source_dir)
        self._stats.symlinks_skipped = scan.symlinks_skipped
        files_to_upload: list[LocalFile] = []

        for lf in scan.files:
            self._stats.files_scanned += 1
            action = self._classify(lf)
            if action == "skip":
                self._stats.files_skipped += 1
            elif action == "size_mismatch":
                self._stats.files_skipped += 1
                self._stats.size_mismatches += 1
            else:
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
    # Classification (main thread only)
    # ------------------------------------------------------------------

    def _classify(self, lf: LocalFile) -> str:
        """Return ``"skip"``, ``"size_mismatch"``, or ``"upload"``."""
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
    # Concurrent upload dispatch (main thread handles stats)
    # ------------------------------------------------------------------

    def _upload_all(self, files: list[LocalFile]) -> None:
        """Upload files using a thread pool with bounded submission.

        At most ``transfers`` futures are outstanding at any time, so memory
        usage stays constant regardless of how many files need uploading.
        """
        logger.info(
            "Uploading %d files with %d workers",
            len(files),
            self._config.transfers,
        )
        file_iter = iter(files)

        with ThreadPoolExecutor(max_workers=self._config.transfers) as pool:
            pending: dict[Future[_WorkerResult], LocalFile] = {}

            # Seed the pool with up to `transfers` tasks.
            for lf in itertools.islice(file_iter, self._config.transfers):
                pending[pool.submit(self._upload_one, lf)] = lf

            while pending:
                done, _ = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    lf = pending.pop(fut)
                    stop = self._handle_result(fut, lf)
                    if stop:
                        pool.shutdown(wait=False, cancel_futures=True)
                        pending.clear()
                        break

                # Submit more work to keep the pool full.
                if not self._daily_limit_hit.is_set():
                    for lf in itertools.islice(
                        file_iter,
                        self._config.transfers - len(pending),
                    ):
                        pending[pool.submit(self._upload_one, lf)] = lf

    def _handle_result(self, fut: Future[_WorkerResult], lf: LocalFile) -> bool:
        """Process a completed future.  Returns True to stop all uploads."""
        try:
            result = fut.result()
        except DailyLimitError:
            self._stats.daily_limit_hits += 1
            self._daily_limit_hit.set()
            logger.error(
                "Daily limit (750 GB) reached. "
                "Re-run the tool after ~24 hours to continue."
            )
            return True
        except Exception as exc:
            self._breaker.record_failure()
            self._stats.files_failed += 1
            self._stats.errors.append(f"{lf.relative_path}: {exc}")
            logger.error("Failed: %s -- %s", lf.relative_path, exc)
            return False

        if result.success:
            self._stats.files_uploaded += 1
            self._stats.bytes_uploaded += result.bytes_uploaded
            if result.resumed:
                self._stats.files_resumed += 1
            self._breaker.record_success()
        else:
            self._stats.files_failed += 1
            if result.error:
                self._stats.errors.append(f"{lf.relative_path}: {result.error}")
            if not result.is_permanent:
                self._breaker.record_failure()
        return False

    # ------------------------------------------------------------------
    # Single-file upload (runs in a worker thread)
    # ------------------------------------------------------------------

    def _upload_one(self, lf: LocalFile) -> _WorkerResult:
        """Upload a single file with retries.

        Returns a ``_WorkerResult`` -- never mutates ``self._stats``.
        """
        for attempt in range(MAX_RETRIES):
            if self._daily_limit_hit.is_set():
                raise DailyLimitError(403, "Daily limit reached")
            try:
                return self._upload_one_attempt(lf)
            except DailyLimitError:
                raise
            except RateLimitError as exc:
                self._backoff(lf, attempt, exc)
            except (DriveApiError, HttpError) as exc:
                status = exc.status if isinstance(exc, DriveApiError) else exc.resp.status
                if 400 <= status < 500 and status not in (401, 429):
                    logger.error("Permanent failure: %s -- %s", lf.relative_path, exc)
                    return _WorkerResult(success=False, error=str(exc), is_permanent=True)
                if status == 401:
                    self._refresh_token()
                self._backoff(lf, attempt, exc)
            except ChecksumError as exc:
                # MD5 mismatch -- always retry (file was trashed, re-upload).
                self._backoff(lf, attempt, exc)
            except OSError as exc:
                # requests.ConnectionError is a subclass of OSError (not
                # builtins.ConnectionError), so network failures land here.
                # Distinguish network errors (retryable) from local I/O
                # errors (permanent).
                if isinstance(exc, requests.exceptions.ConnectionError):
                    self._backoff(lf, attempt, exc)
                else:
                    logger.error(
                        "Local read error: %s -- %s", lf.relative_path, exc
                    )
                    return _WorkerResult(
                        success=False, error=str(exc), is_permanent=True
                    )

        logger.error("Max retries exceeded: %s", lf.relative_path)
        return _WorkerResult(success=False, error="max retries exceeded")

    def _refresh_token(self) -> None:
        """Attempt to refresh the OAuth token."""
        try:
            self._drive._creds.refresh(Request())
            logger.info("OAuth token refreshed")
        except Exception as exc:
            logger.warning("Token refresh failed: %s", exc)

    @staticmethod
    def _backoff(lf: LocalFile, attempt: int, exc: Exception) -> None:
        delay = min(60, 2 ** attempt) * random.random()
        logger.warning(
            "%s (attempt %d/%d), backing off %.1fs: %s",
            lf.relative_path, attempt + 1, MAX_RETRIES, delay, exc,
        )
        time.sleep(delay)

    def _upload_one_attempt(self, lf: LocalFile) -> _WorkerResult:
        """Single upload attempt."""
        parent_id = self._ensure_parent_folder(lf.relative_path)
        if lf.size <= MULTIPART_THRESHOLD:
            return self._upload_small(lf, parent_id)
        return self._upload_resumable(lf, parent_id)

    # ------------------------------------------------------------------
    # MD5 verification (shared by small + resumable paths)
    # ------------------------------------------------------------------

    def _verify_md5(
        self, lf: LocalFile, local_md5: str, response: UploadResponse
    ) -> None:
        """Compare local MD5 with Drive's.  Trash and raise on mismatch."""
        if not self._config.verify_checksum or not response.md5_checksum:
            return
        if local_md5 == response.md5_checksum:
            return
        logger.error(
            "MD5 mismatch for %s: local=%s drive=%s",
            lf.relative_path, local_md5, response.md5_checksum,
        )
        self._drive.trash_file(response.file_id)
        raise ChecksumError(f"MD5 mismatch: {lf.relative_path}")

    # ------------------------------------------------------------------
    # Small-file upload
    # ------------------------------------------------------------------

    def _upload_small(self, lf: LocalFile, parent_id: str) -> _WorkerResult:
        """Upload a file <= 8 MiB with a single multipart request."""
        logger.info("Uploading (small): %s (%d bytes)", lf.relative_path, lf.size)

        result = self._drive.multipart_upload(
            file_path=lf.path,
            name=lf.path.name,
            parent_id=parent_id,
            created_time=lf.ctime,
            modified_time=lf.mtime,
        )
        local_md5 = hashlib.md5(lf.path.read_bytes()).hexdigest()
        self._verify_md5(lf, local_md5, result)

        self._session_cache.remove(lf.relative_path)
        logger.info("Uploaded: %s", lf.relative_path)
        return _WorkerResult(success=True, bytes_uploaded=lf.size)

    # ------------------------------------------------------------------
    # Resumable upload
    # ------------------------------------------------------------------

    def _upload_resumable(self, lf: LocalFile, parent_id: str) -> _WorkerResult:
        """Upload a file > 8 MiB using the resumable upload protocol."""
        session_uri, resume_offset, resumed = self._try_resume(lf)

        # Session was already completed elsewhere -- no bytes transferred this run.
        if resume_offset == lf.size:
            return _WorkerResult(success=True, bytes_uploaded=0, resumed=True)

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
                SessionEntry(session_uri=session_uri, file_size=lf.size, mtime=lf.mtime),
            )

        # Stream chunks while computing MD5.
        hasher = hashlib.md5()
        chunk_size = self._config.chunk_size
        offset = 0
        resp: UploadResponse | None = None

        with open(lf.path, "rb") as f:
            # Hash the already-uploaded portion for MD5 continuity.
            if resume_offset > 0:
                remaining = resume_offset
                while remaining > 0:
                    block = f.read(min(chunk_size, remaining))
                    if not block:
                        break
                    hasher.update(block)
                    remaining -= len(block)
                offset = resume_offset

            while offset < lf.size:
                data = f.read(chunk_size)
                if not data:
                    break
                hasher.update(data)

                chunk_start = time.monotonic()
                resp = self._drive.upload_chunk(
                    session_uri=session_uri, data=data, start=offset, total=lf.size,
                )
                offset += len(data)

                if self._config.bwlimit:
                    elapsed = time.monotonic() - chunk_start
                    expected = len(data) / self._config.bwlimit
                    if elapsed < expected:
                        time.sleep(expected - elapsed)

        if resp is None:
            raise DriveApiError(
                500, f"Upload produced no completion response: {lf.relative_path}"
            )

        local_md5 = hasher.hexdigest()
        self._verify_md5(lf, local_md5, resp)

        self._session_cache.remove(lf.relative_path)
        logger.info("Uploaded: %s (%s)", lf.relative_path, local_md5)
        return _WorkerResult(success=True, bytes_uploaded=lf.size, resumed=resumed)

    def _try_resume(self, lf: LocalFile) -> tuple[str | None, int, bool]:
        """Check the session cache for a resumable session.

        Returns ``(session_uri, confirmed_bytes, resumed)``.  If the upload
        was already completed (e.g. from another machine), returns
        ``(None, file_size, True)`` so the caller can return immediately.
        """
        cached = self._session_cache.get(lf.relative_path)
        if cached is None:
            return None, 0, False

        if cached.file_size != lf.size or cached.mtime != lf.mtime:
            logger.warning("Discarding stale session for %s (file changed)", lf.relative_path)
            self._session_cache.remove(lf.relative_path)
            return None, 0, False

        try:
            confirmed = self._drive.query_upload_status(cached.session_uri, lf.size)
        except DriveApiError:
            logger.info("Stale session for %s (expired or invalid), starting fresh", lf.relative_path)
            self._session_cache.remove(lf.relative_path)
            return None, 0, False

        if confirmed == lf.size:
            logger.info("Session already completed: %s", lf.relative_path)
            self._session_cache.remove(lf.relative_path)
            return None, lf.size, True  # signal: already done

        logger.info("Resuming %s from byte %d / %d", lf.relative_path, confirmed, lf.size)
        return cached.session_uri, confirmed, True

    # ------------------------------------------------------------------
    # Folder management (thread-safe)
    # ------------------------------------------------------------------

    def _ensure_parent_folder(self, relative_path: str) -> str:
        """Return the Drive folder ID for the immediate parent.

        Creates missing folders on Drive.  Thread-safe via ``_folder_lock``.
        """
        parts = relative_path.split("/")[:-1]
        if not parts:
            return self._config.drive_folder_id

        current_path = ""
        parent_id = self._config.drive_folder_id

        with self._folder_lock:
            for part in parts:
                current_path = f"{current_path}{part}/" if current_path else f"{part}/"
                if current_path in self._folder_map:
                    parent_id = self._folder_map[current_path]
                else:
                    parent_id = self._drive.create_folder(part, parent_id)
                    self._folder_map[current_path] = parent_id
                    logger.info("Created folder: %s", current_path)

        return parent_id
