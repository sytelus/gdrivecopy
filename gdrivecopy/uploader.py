"""Shared upload protocol engine and compatibility upload orchestration.

Modern jobs call ``upload_file`` with durable identity/session/folder adapters;
``TransferRunner`` owns their manifests, dispatch and reports. ``run`` retains
the original in-memory scan/upload/report workflow for ``legacy-upload``.

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
import stat
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests.exceptions
from googleapiclient.errors import HttpError

from gdrivecopy.control import RunControl
from gdrivecopy.drive import (
    MULTIPART_THRESHOLD,
    DriveApiError,
    DriveClient,
    QuotaLimitError,
    RateLimitError,
    UploadResponse,
    UploadSessionError,
    UploadStatus,
)
from gdrivecopy.models import (
    DriveFile,
    LocalFile,
    SessionEntry,
    UploadConfig,
    UploadStats,
)
from gdrivecopy.scanner import _is_link_like, _iso_from_timestamp, scan_local
from gdrivecopy.session import SessionCache

logger = logging.getLogger(__name__)


class ChecksumError(Exception):
    """Raised when the post-upload MD5 does not match the local file."""


class SourceFileChangedError(OSError):
    """Raised when a source file changes after the initial scan."""


class CleanupError(Exception):
    """Raised when an unverified Drive item cannot be moved to trash safely."""


class AmbiguousMultipartError(Exception):
    """Raised when a one-request upload may have succeeded despite an error."""


# ------------------------------------------------------------------
# Worker result (returned by _upload_one, consumed by main thread)
# ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _WorkerResult:
    """Immutable result returned by a worker thread."""

    success: bool
    bytes_uploaded: int = 0
    resumed: bool = False
    error: str | None = None
    is_permanent: bool = False
    file_id: str | None = None
    md5_checksum: str | None = None


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


class _BandwidthLimiter:
    """Coordinate a process-wide average upload-rate limit across workers."""

    def __init__(self, bytes_per_second: int | None) -> None:
        self._rate = bytes_per_second
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def wait_for_slot(self, byte_count: int, stop_event: threading.Event | None = None) -> None:
        """Wait until *byte_count* bytes fit in the shared transfer schedule."""
        if self._rate is None or byte_count <= 0:
            return

        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + byte_count / self._rate

        delay = slot - now
        if delay > 0:
            if stop_event is None:
                time.sleep(delay)
            else:
                stop_event.wait(delay)


# ------------------------------------------------------------------
# Uploader
# ------------------------------------------------------------------


class Uploader:
    """Orchestrates the full upload workflow.

    Args:
        config: Upload configuration (CLI arguments).
        drive: Authenticated Drive API client.
    """

    def __init__(
        self,
        config: UploadConfig,
        drive: DriveClient,
        *,
        control: RunControl | None = None,
        sessions=None,
        folder_map=None,
        reserve_id=None,
        discard_id=None,
    ) -> None:
        if not config.drive_folder_id.strip():
            raise ValueError("drive_folder_id must not be empty")
        if config.transfers < 1:
            raise ValueError("transfers must be at least 1")
        if config.chunk_size < 256 * 1024 or config.chunk_size % (256 * 1024):
            raise ValueError("chunk_size must be a multiple of 256 KiB")
        if config.retries < 1:
            raise ValueError("retries must be at least 1")
        if config.bwlimit is not None and config.bwlimit < 1:
            raise ValueError("bwlimit must be greater than zero")

        self._config = config
        self._drive = drive
        self._session_cache = (
            sessions if sessions is not None else SessionCache(config.session_path)
        )
        self.control = control
        self._reserve_id, self._discard_id = reserve_id, discard_id
        self._stats = UploadStats()
        self._breaker = _CircuitBreaker()
        self._bandwidth_limiter = _BandwidthLimiter(config.bwlimit)
        self._quota_limit_hit = threading.Event()

        # Populated during Phase 1, guarded by _folder_lock for writes.
        self._file_map: dict[str, DriveFile] = {}
        self._folder_map = folder_map if folder_map is not None else {}
        self._folder_lock = threading.Lock()
        self._root_folder_id = config.drive_folder_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> UploadStats:
        """Execute the full scan -> upload -> report workflow."""
        start = time.monotonic()
        self._stats = UploadStats()
        self._breaker = _CircuitBreaker()
        self._bandwidth_limiter = _BandwidthLimiter(self._config.bwlimit)
        self._quota_limit_hit.clear()

        # Phase 1: Scan Drive.
        logger.info("Phase 1: Scanning Drive folder %s", self._config.drive_folder_id)
        self._file_map, self._folder_map = self._drive.list_all(self._config.drive_folder_id)
        self._root_folder_id = self._folder_map[""]
        self._session_cache.load()

        # Phase 2: Scan local & decide.
        logger.info("Phase 2: Scanning local directory %s", self._config.source_dir)
        owned_paths = {
            self._config.credentials_path.resolve(),
            self._config.token_path.resolve(),
            self._config.session_path.resolve(),
            (self._config.log_dir / "report.json").resolve(),
        }
        if self._config.log_path is not None:
            owned_paths.add(self._config.log_path.resolve())
        excluded_paths = owned_paths | {path.with_name(f"{path.name}.tmp") for path in owned_paths}
        scan = scan_local(self._config.source_dir, excluded_paths)
        self._stats.symlinks_skipped = scan.symlinks_skipped
        self._stats.files_excluded = scan.files_excluded
        self._stats.scan_errors = len(scan.errors)
        self._stats.errors.extend(scan.errors)
        files_to_upload: list[LocalFile] = []

        for lf in scan.files:
            self._stats.files_scanned += 1
            action = self._classify(lf)
            if action == "skip":
                self._stats.files_skipped += 1
            elif action == "size_mismatch":
                self._stats.files_skipped += 1
                self._stats.size_mismatches += 1
            elif action == "path_conflict":
                self._stats.files_skipped += 1
                self._stats.path_conflicts += 1
            else:
                files_to_upload.append(lf)
                if self._config.dry_run:
                    logger.info("Would upload: %s (%d bytes)", lf.relative_path, lf.size)

        self._stats.files_to_upload = len(files_to_upload)

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
        """Classify without introducing file/folder collisions on Drive."""
        parts = lf.relative_path.split("/")
        ancestors = ("/".join(parts[:i]) for i in range(1, len(parts)))
        collision = next((path for path in ancestors if path in self._file_map), None)
        if f"{lf.relative_path}/" in self._folder_map:
            collision = lf.relative_path
        if collision is not None:
            detail = f"{lf.relative_path}: file/folder conflict at Drive path {collision!r}"
            logger.error(detail)
            self._stats.errors.append(detail)
            return "path_conflict"
        drive_file = self._file_map.get(lf.relative_path)
        if drive_file is None:
            return "upload"
        if not self._config.dry_run and self._session_cache.get(lf.relative_path) is not None:
            # Drive is authoritative.  A visible item makes any leftover
            # resumable URI obsolete, including one left after a lost final
            # response in an earlier process.
            self._session_cache.remove(lf.relative_path)
        if drive_file.size == lf.size:
            logger.debug("Skipping (on Drive, same size): %s", lf.relative_path)
            return "skip"

        drive_size = f"{drive_file.size} bytes" if drive_file.size is not None else "unknown"
        detail = f"{lf.relative_path}: local={lf.size} bytes, drive={drive_size}"
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
            stop_submitting = False

            # Seed the pool with up to `transfers` tasks.
            for lf in itertools.islice(file_iter, self._config.transfers):
                pending[pool.submit(self._upload_one, lf)] = lf

            while pending:
                done, _ = concurrent.futures.wait(
                    pending,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    lf = pending.pop(fut)
                    stop_submitting |= self._handle_result(fut, lf)

                if stop_submitting:
                    # Running requests cannot be forcefully interrupted, but
                    # queued work can be canceled.  Keep draining running
                    # futures so completed work is reflected in the report.
                    for fut in list(pending):
                        if fut.cancel():
                            pending.pop(fut)

                # Submit more work to keep the pool full.
                if not stop_submitting:
                    for lf in itertools.islice(
                        file_iter,
                        self._config.transfers - len(pending),
                    ):
                        pending[pool.submit(self._upload_one, lf)] = lf

    def _handle_result(self, fut: Future[_WorkerResult], lf: LocalFile) -> bool:
        """Process a completed future.  Returns True to stop all uploads."""
        try:
            result = fut.result()
        except QuotaLimitError:
            first_hit = self._stats.quota_limit_hits == 0
            self._quota_limit_hit.set()
            if first_hit:
                self._stats.quota_limit_hits = 1
                logger.error(
                    "A nonretryable Drive storage, item, or daily API quota was reached. "
                    "Review the Drive account and Google Cloud quota settings before retrying."
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
        auth_retried = False
        for attempt in range(self._config.retries):
            if self.control is not None:
                self.control.check()
            if self._quota_limit_hit.is_set():
                raise QuotaLimitError(403, "Blocking Drive quota reached")
            try:
                return self._upload_one_attempt(lf)
            except QuotaLimitError:
                self._quota_limit_hit.set()
                raise
            except RateLimitError as exc:
                self._backoff(lf, attempt, exc)
            except UploadSessionError as exc:
                # The server explicitly rejected this resumable session. Clear
                # it before retrying so the next attempt creates a new one.
                self._session_cache.remove(lf.relative_path)
                self._backoff(lf, attempt, exc)
            except (DriveApiError, HttpError) as exc:
                status = exc.status if isinstance(exc, DriveApiError) else exc.resp.status
                if status == 401:
                    if auth_retried:
                        logger.error("Auth failure after token refresh: %s", lf.relative_path)
                        return _WorkerResult(success=False, error=str(exc), is_permanent=True)
                    refresh_error = self._refresh_token()
                    if refresh_error is not None:
                        return _WorkerResult(
                            success=False,
                            error=refresh_error,
                            is_permanent=True,
                        )
                    auth_retried = True
                    continue  # retry immediately, no backoff
                if 400 <= status < 500 and status not in (408, 429):
                    logger.error("Permanent failure: %s -- %s", lf.relative_path, exc)
                    return _WorkerResult(success=False, error=str(exc), is_permanent=True)
                self._backoff(lf, attempt, exc)
            except ChecksumError as exc:
                # MD5 mismatch -- always retry (file was trashed, re-upload).
                self._backoff(lf, attempt, exc)
            except CleanupError as exc:
                # Retrying could create a duplicate because the unverified
                # Drive item may still exist.  Stop this file for manual review.
                logger.error("Cleanup failure: %s -- %s", lf.relative_path, exc)
                return _WorkerResult(success=False, error=str(exc), is_permanent=True)
            except AmbiguousMultipartError as exc:
                # A one-request upload has no status endpoint. The file may
                # exist even though its response was lost, so retrying now can
                # create a duplicate. A later run will reconcile Drive first.
                logger.error("Ambiguous multipart result: %s -- %s", lf.relative_path, exc)
                return _WorkerResult(success=False, error=str(exc), is_permanent=True)
            except requests.exceptions.RequestException as exc:
                # Timeouts, TLS failures, and connection resets are all
                # transport errors and are safe to retry via the resumable
                # session status check on the next attempt.
                self._backoff(lf, attempt, exc)
            except OSError as exc:
                logger.error("Local read error: %s -- %s", lf.relative_path, exc)
                return _WorkerResult(success=False, error=str(exc), is_permanent=True)

        logger.error("Max retries exceeded: %s", lf.relative_path)
        return _WorkerResult(success=False, error="max retries exceeded")

    def _refresh_token(self) -> str | None:
        """Refresh the OAuth token and return an error message on failure."""
        try:
            self._drive.refresh_credentials()
            logger.info("OAuth token refreshed")
            return None
        except Exception as exc:
            message = f"OAuth token refresh failed: {exc}"
            logger.error(message)
            return message

    def _backoff(self, lf: LocalFile, attempt: int, exc: Exception) -> None:
        if attempt + 1 >= self._config.retries:
            return  # No pointless sleep after the final failed attempt.
        delay = min(60, 2**attempt) * random.random()
        logger.warning(
            "%s (attempt %d/%d), backing off %.1fs: %s",
            lf.relative_path,
            attempt + 1,
            self._config.retries,
            delay,
            exc,
        )
        if self.control:
            self.control.emit(
                "retry",
                lf.relative_path,
                attempt=attempt + 1,
                delay=delay,
                message=type(exc).__name__,
            )
            self.control.wait(delay)
        else:
            time.sleep(delay)

    def _upload_one_attempt(self, lf: LocalFile) -> _WorkerResult:
        """Single upload attempt."""
        self._assert_source_unchanged(lf)
        parent_id = self._ensure_parent_folder(lf.relative_path)
        if lf.size <= MULTIPART_THRESHOLD:
            return self._upload_small(lf, parent_id)
        return self._upload_resumable(lf, parent_id)

    # ------------------------------------------------------------------
    # MD5 verification (shared by small + resumable paths)
    # ------------------------------------------------------------------

    def _verify_md5(self, lf: LocalFile, local_md5: str, response: UploadResponse) -> None:
        """Compare local MD5 with Drive's.  Trash and raise on mismatch."""
        self._assert_upload_identity(lf, response)
        if not self._config.verify_checksum:
            return
        if not response.md5_checksum:
            logger.error("Drive omitted MD5 checksum for %s", lf.relative_path)
            self._trash_unverified(lf, response, "Drive omitted its MD5 checksum")
            raise ChecksumError(f"Drive omitted MD5 checksum: {lf.relative_path}")
        if local_md5 == response.md5_checksum:
            return
        logger.error(
            "MD5 mismatch for %s: local=%s drive=%s",
            lf.relative_path,
            local_md5,
            response.md5_checksum,
        )
        self._trash_unverified(lf, response, "MD5 checksum mismatch")
        raise ChecksumError(f"MD5 mismatch: {lf.relative_path}")

    def _trash_unverified(self, lf: LocalFile, response: UploadResponse, reason: str) -> None:
        """Trash an unsafe upload or raise without risking a duplicate retry."""
        self._assert_upload_identity(lf, response)
        try:
            self._drive.trash_file(response.file_id)
        except Exception as exc:
            raise CleanupError(
                f"{reason}; could not trash Drive item {response.file_id}: {exc}"
            ) from exc
        if self._discard_id is not None:
            self._discard_id(lf)

    def _assert_upload_identity(self, lf: LocalFile, response: UploadResponse) -> None:
        # Check before both accepting and cleaning up a response. Checking only
        # in the job runner is too late: checksum failure may already trash it.
        if self._reserve_id is not None and response.file_id != self._reserve_id(lf):
            raise CleanupError(
                "Drive returned a different upload identity than reserved; no item was trashed"
            )

    @staticmethod
    def _new_md5() -> Any:
        """Create an MD5 hasher for Drive integrity checks, not security."""
        return hashlib.md5(usedforsecurity=False)

    def _file_md5(self, path: Path) -> str:
        """Hash a local file without loading a second full copy into memory."""
        hasher = self._new_md5()
        with path.open("rb") as file_obj:
            for block in iter(lambda: file_obj.read(1024 * 1024), b""):
                if self.control is not None:
                    self.control.check()
                hasher.update(block)
        return hasher.hexdigest()

    @staticmethod
    def _assert_source_unchanged(lf: LocalFile) -> None:
        """Fail safely if a source file changed since it was scanned."""
        try:
            if _is_link_like(lf.path):
                raise SourceFileChangedError(f"Source was replaced by a link: {lf.relative_path}")
            current = lf.path.stat()
        except OSError as exc:
            raise SourceFileChangedError(
                f"Cannot re-inspect source file during upload: {lf.relative_path}: {exc}"
            ) from exc
        changed = not stat.S_ISREG(current.st_mode) or current.st_size != lf.size
        if lf.device is not None and lf.inode is not None:
            changed = changed or (current.st_dev, current.st_ino) != (lf.device, lf.inode)
        if lf.mtime_ns is not None:
            changed = changed or current.st_mtime_ns != lf.mtime_ns
        else:
            changed = changed or _iso_from_timestamp(current.st_mtime) != lf.mtime
        if changed:
            raise SourceFileChangedError(f"Source file changed during upload: {lf.relative_path}")

    # ------------------------------------------------------------------
    # Small-file upload
    # ------------------------------------------------------------------

    def _upload_small(self, lf: LocalFile, parent_id: str) -> _WorkerResult:
        """Upload a file <= 8 MiB with a single multipart request."""
        logger.info("Uploading (small): %s (%d bytes)", lf.relative_path, lf.size)

        # Hash before creating the Drive item so a local read failure cannot
        # leave an uploaded file that a later size-only scan would skip.
        local_md5 = self._file_md5(lf.path) if self._config.verify_checksum else ""
        self._assert_source_unchanged(lf)
        self._bandwidth_limiter.wait_for_slot(
            lf.size, self.control.stop if self.control else self._quota_limit_hit
        )
        if self.control is not None:
            self.control.check()
        if self._quota_limit_hit.is_set():
            raise QuotaLimitError(403, "Blocking Drive quota reached")
        try:
            result = self._drive.multipart_upload(
                file_path=lf.path,
                name=lf.path.name,
                parent_id=parent_id,
                created_time=lf.ctime,
                modified_time=lf.mtime,
                **({"file_id": self._reserve_id(lf)} if self._reserve_id else {}),
            )
        except (QuotaLimitError, RateLimitError):
            raise
        except requests.exceptions.RequestException as exc:
            if self._reserve_id is not None:
                raise  # Replaying the same durable ID cannot create a duplicate.
            raise AmbiguousMultipartError(
                "small-file upload response was lost; rerun to reconcile Drive before retrying"
            ) from exc
        except DriveApiError as exc:
            if exc.status == 408 or exc.status >= 500:
                if self._reserve_id is not None:
                    raise
                raise AmbiguousMultipartError(
                    "small-file upload had an ambiguous server response; rerun to "
                    "reconcile Drive before retrying"
                ) from exc
            raise
        try:
            self._assert_source_unchanged(lf)
        except SourceFileChangedError as exc:
            self._trash_unverified(lf, result, str(exc))
            raise
        self._verify_md5(lf, local_md5, result)

        self._session_cache.remove(lf.relative_path)
        logger.info("Uploaded: %s", lf.relative_path)
        if self.control:
            self.control.emit(
                "progress", lf.relative_path, offset=lf.size, size=lf.size, bytes=lf.size
            )
        return _WorkerResult(
            success=True,
            bytes_uploaded=lf.size,
            file_id=result.file_id,
            md5_checksum=result.md5_checksum,
        )

    # ------------------------------------------------------------------
    # Resumable upload
    # ------------------------------------------------------------------

    def _upload_resumable(self, lf: LocalFile, parent_id: str) -> _WorkerResult:
        """Upload a file > 8 MiB using the resumable upload protocol."""
        session_uri, status, resumed = self._try_resume(lf, parent_id)
        resume_offset = status.confirmed_bytes

        # A previous request may have completed on Drive after its local HTTP
        # response was lost.  Verify that completed item before accepting it.
        if status.completed is not None:
            self._session_cache.remove(lf.relative_path)
            try:
                local_md5 = self._file_md5(lf.path) if self._config.verify_checksum else ""
                self._assert_source_unchanged(lf)
            except OSError as exc:
                self._trash_unverified(lf, status.completed, str(exc))
                raise
            self._verify_md5(lf, local_md5, status.completed)
            return _WorkerResult(
                success=True,
                bytes_uploaded=0,
                resumed=True,
                file_id=status.completed.file_id,
                md5_checksum=status.completed.md5_checksum,
            )

        if session_uri is None:
            reserved_id = self._reserve_id(lf) if self._reserve_id else None
            try:
                session_uri = self._drive.initiate_resumable_upload(
                    name=lf.path.name,
                    parent_id=parent_id,
                    file_size=lf.size,
                    created_time=lf.ctime,
                    modified_time=lf.mtime,
                    **({"file_id": reserved_id} if reserved_id else {}),
                )
            except DriveApiError as exc:
                if exc.status != 409 or reserved_id is None:
                    raise
                completed = self._drive.upload_result(reserved_id)
                try:
                    digest = self._file_md5(lf.path) if self._config.verify_checksum else ""
                    self._assert_source_unchanged(lf)
                except OSError as error:
                    self._trash_unverified(lf, completed, str(error))
                    raise
                self._verify_md5(lf, digest, completed)
                self._session_cache.remove(lf.relative_path)
                return _WorkerResult(
                    success=True,
                    resumed=True,
                    file_id=completed.file_id,
                    md5_checksum=completed.md5_checksum,
                )
            self._session_cache.put(
                lf.relative_path,
                SessionEntry(
                    session_uri=session_uri,
                    file_size=lf.size,
                    mtime=lf.mtime,
                    source_path=str(lf.path),
                    parent_id=parent_id,
                    mtime_ns=lf.mtime_ns,
                ),
            )

        # Stream chunks while computing MD5.
        hasher = self._new_md5() if self._config.verify_checksum else None
        chunk_size = self._config.chunk_size
        offset = 0
        resp: UploadResponse | None = None

        with open(lf.path, "rb") as f:
            # Hash the already-uploaded portion for MD5 continuity.
            if resume_offset > 0:
                remaining = resume_offset if hasher is not None else 0
                if hasher is None:
                    f.seek(resume_offset)
                while remaining > 0:
                    if self.control:
                        self.control.check()
                    block = f.read(min(chunk_size, remaining))
                    if not block:
                        raise SourceFileChangedError(
                            f"Source file became shorter during upload: {lf.relative_path}"
                        )
                    if hasher is not None:
                        hasher.update(block)
                    remaining -= len(block)
                    if self.control:
                        self.control.emit(
                            "progress",
                            lf.relative_path,
                            offset=resume_offset - remaining,
                            size=lf.size,
                            status="Checking resume data",
                        )
                offset = resume_offset

            while offset < lf.size:
                if self.control:
                    self.control.check()
                if self._quota_limit_hit.is_set():
                    raise QuotaLimitError(403, "Blocking Drive quota reached")
                # Never read beyond the size declared when the resumable
                # session was created. If the file grows concurrently, finish
                # only the original byte range and reject it in the post-check.
                data = f.read(min(chunk_size, lf.size - offset))
                if not data:
                    raise SourceFileChangedError(
                        f"Source file became shorter during upload: {lf.relative_path}"
                    )
                if hasher is not None:
                    hasher.update(data)

                self._bandwidth_limiter.wait_for_slot(
                    len(data), self.control.stop if self.control else self._quota_limit_hit
                )
                if self.control:
                    self.control.check()
                if self._quota_limit_hit.is_set():
                    raise QuotaLimitError(403, "Blocking Drive quota reached")
                resp = self._drive.upload_chunk(
                    session_uri=session_uri,
                    data=data,
                    start=offset,
                    total=lf.size,
                )
                offset += len(data)
                if self.control:
                    self.control.emit(
                        "progress",
                        lf.relative_path,
                        offset=offset,
                        size=lf.size,
                        bytes=len(data),
                        status="Uploading",
                    )

        if resp is None:
            raise DriveApiError(500, f"Upload produced no completion response: {lf.relative_path}")

        local_md5 = hasher.hexdigest() if hasher is not None else ""
        # Remove session BEFORE verifying MD5 so a ChecksumError retry
        # doesn't find a stale "completed" session and report false success.
        self._session_cache.remove(lf.relative_path)
        try:
            self._assert_source_unchanged(lf)
        except SourceFileChangedError as exc:
            self._trash_unverified(lf, resp, str(exc))
            raise
        self._verify_md5(lf, local_md5, resp)

        logger.info("Uploaded: %s (%s)", lf.relative_path, local_md5)
        return _WorkerResult(
            success=True,
            bytes_uploaded=lf.size - resume_offset,
            resumed=resumed,
            file_id=resp.file_id,
            md5_checksum=resp.md5_checksum,
        )

    def _try_resume(self, lf: LocalFile, parent_id: str) -> tuple[str | None, UploadStatus, bool]:
        """Check the session cache for a resumable session.

        Returns ``(session_uri, status, resumed)``.  Completed status includes
        the Drive metadata required for checksum verification.
        """
        cached = self._session_cache.get(lf.relative_path)
        if cached is None:
            return None, UploadStatus(confirmed_bytes=0), False

        if (
            cached.file_size != lf.size
            or cached.mtime != lf.mtime
            or cached.mtime_ns != lf.mtime_ns
            or cached.source_path is None
            or Path(cached.source_path) != lf.path
            or cached.parent_id != parent_id
        ):
            # Relative path, size and mtime alone can match an unrelated tree.
            # A session permanently targets the parent chosen at initiation.
            logger.warning("Discarding stale or unscoped session for %s", lf.relative_path)
            self._session_cache.remove(lf.relative_path)
            return None, UploadStatus(confirmed_bytes=0), False

        try:
            status = self._drive.query_upload_status(cached.session_uri, lf.size)
        except UploadSessionError:
            logger.info(
                "Stale session for %s (expired or invalid), starting fresh",
                lf.relative_path,
            )
            self._session_cache.remove(lf.relative_path)
            return None, UploadStatus(confirmed_bytes=0), False

        if status.completed is not None:
            logger.info("Session already completed: %s", lf.relative_path)
            return None, status, True

        logger.info(
            "Resuming %s from byte %d / %d",
            lf.relative_path,
            status.confirmed_bytes,
            lf.size,
        )
        return cached.session_uri, status, True

    # ------------------------------------------------------------------
    # Folder management (thread-safe)
    # ------------------------------------------------------------------

    def _ensure_parent_folder(self, relative_path: str) -> str:
        """Return the Drive folder ID for the immediate parent.

        Creates missing folders on Drive.  Thread-safe via ``_folder_lock``.
        """
        parts = relative_path.split("/")[:-1]
        if not parts:
            return self._root_folder_id

        current_path = ""
        parent_id = self._root_folder_id

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

    def upload_file(self, local_file: LocalFile) -> _WorkerResult:
        """Transfer one manifest item; the job runner owns scheduling and reports."""
        return self._upload_one(local_file)
