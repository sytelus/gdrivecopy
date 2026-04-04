"""Tests for gdrivecopy.uploader -- upload orchestration."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

from gdrivecopy.drive import (
    DailyLimitError,
    DriveApiError,
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
from gdrivecopy.uploader import MULTIPART_THRESHOLD, Uploader, _CircuitBreaker


# ---------------------------------------------------------------------------
# Classification / skip detection
# ---------------------------------------------------------------------------


class TestClassification:
    def test_file_not_on_drive_is_upload(
        self, upload_config: UploadConfig, mock_drive: MagicMock
    ) -> None:
        """A local file not found in the Drive map should be classified as 'upload'."""
        mock_drive.list_all.return_value = ({}, {"": "root-folder-id"})

        uploader = Uploader(upload_config, mock_drive)
        # Simulate populating file_map like run() does
        uploader._file_map = {}

        lf = LocalFile(
            path=Path("/fake/new.txt"),
            relative_path="new.txt",
            size=100,
            mtime="2025-01-01T00:00:00+00:00",
        )
        assert uploader._classify(lf) == "upload"

    def test_file_exists_same_size_is_skip(
        self, upload_config: UploadConfig, mock_drive: MagicMock
    ) -> None:
        """A file present on Drive with matching size should be skipped."""
        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {
            "existing.txt": DriveFile(id="d1", name="existing.txt", size=100)
        }

        lf = LocalFile(
            path=Path("/fake/existing.txt"),
            relative_path="existing.txt",
            size=100,
            mtime="2025-01-01T00:00:00+00:00",
        )
        assert uploader._classify(lf) == "skip"

    def test_file_exists_different_size_is_mismatch(
        self, upload_config: UploadConfig, mock_drive: MagicMock
    ) -> None:
        """A file present on Drive with different size is a size_mismatch."""
        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {
            "changed.txt": DriveFile(id="d1", name="changed.txt", size=50)
        }

        lf = LocalFile(
            path=Path("/fake/changed.txt"),
            relative_path="changed.txt",
            size=100,
            mtime="2025-01-01T00:00:00+00:00",
        )
        result = uploader._classify(lf)
        assert result == "size_mismatch"
        assert len(uploader._stats.mismatch_details) == 1


# ---------------------------------------------------------------------------
# Small file upload (multipart)
# ---------------------------------------------------------------------------


class TestSmallFileUpload:
    def test_small_file_uses_multipart(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """Files at or below MULTIPART_THRESHOLD use multipart_upload."""
        content = b"small file data"
        lf = make_local_file(name="small.txt", content=content)

        # Set the md5 to match so checksum verification passes
        md5 = hashlib.md5(content).hexdigest()
        mock_drive.multipart_upload.return_value = UploadResponse(
            file_id="mp-id", md5_checksum=md5
        )

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        result = uploader._upload_small(lf, "root-folder-id")
        assert result.success is True
        assert result.bytes_uploaded == len(content)
        mock_drive.multipart_upload.assert_called_once()


# ---------------------------------------------------------------------------
# Resumable upload
# ---------------------------------------------------------------------------


class TestResumableUpload:
    def test_new_resumable_upload(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """A large file with no cached session initiates a new resumable upload."""
        content = b"x" * (MULTIPART_THRESHOLD + 1)
        lf = make_local_file(name="big.bin", content=content)

        md5 = hashlib.md5(content).hexdigest()
        mock_drive.upload_chunk.return_value = UploadResponse(
            file_id="res-id", md5_checksum=md5
        )

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        result = uploader._upload_resumable(lf, "root-folder-id")
        assert result.success is True
        assert result.bytes_uploaded == len(content)
        mock_drive.initiate_resumable_upload.assert_called_once()


# ---------------------------------------------------------------------------
# Session resume
# ---------------------------------------------------------------------------


class TestSessionResume:
    def test_valid_session_resumes(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """A valid cached session resumes from the confirmed byte offset."""
        content = b"a" * (MULTIPART_THRESHOLD + 1024)
        lf = make_local_file(name="resume.bin", content=content)

        md5 = hashlib.md5(content).hexdigest()
        mock_drive.query_upload_status.return_value = 512
        mock_drive.upload_chunk.return_value = UploadResponse(
            file_id="res-id", md5_checksum=md5
        )

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        # Pre-populate the session cache
        uploader._session_cache.put(
            "resume.bin",
            SessionEntry(
                session_uri="https://upload.example.com/resume-session",
                file_size=lf.size,
                mtime=lf.mtime,
            ),
        )

        result = uploader._upload_resumable(lf, "root-folder-id")
        assert result.success is True
        assert result.resumed is True
        # Should NOT initiate a new session
        mock_drive.initiate_resumable_upload.assert_not_called()

    def test_stale_session_starts_fresh(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """If the file changed since the session was created, start a new upload."""
        content = b"b" * (MULTIPART_THRESHOLD + 1024)
        lf = make_local_file(name="stale.bin", content=content)

        md5 = hashlib.md5(content).hexdigest()
        mock_drive.upload_chunk.return_value = UploadResponse(
            file_id="res-id", md5_checksum=md5
        )

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        # Session has a different file_size -- triggers stale detection
        uploader._session_cache.put(
            "stale.bin",
            SessionEntry(
                session_uri="https://upload.example.com/stale",
                file_size=lf.size + 999,  # different size
                mtime=lf.mtime,
            ),
        )

        result = uploader._upload_resumable(lf, "root-folder-id")
        assert result.success is True
        # Must initiate a new session since old one was stale
        mock_drive.initiate_resumable_upload.assert_called_once()

    def test_expired_session_starts_fresh(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """If query_upload_status raises (session expired), start fresh."""
        content = b"c" * (MULTIPART_THRESHOLD + 1024)
        lf = make_local_file(name="expired.bin", content=content)

        md5 = hashlib.md5(content).hexdigest()
        mock_drive.query_upload_status.side_effect = DriveApiError(404, "Not Found")
        mock_drive.upload_chunk.return_value = UploadResponse(
            file_id="new-id", md5_checksum=md5
        )

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        uploader._session_cache.put(
            "expired.bin",
            SessionEntry(
                session_uri="https://upload.example.com/expired",
                file_size=lf.size,
                mtime=lf.mtime,
            ),
        )

        result = uploader._upload_resumable(lf, "root-folder-id")
        assert result.success is True
        mock_drive.initiate_resumable_upload.assert_called_once()

    def test_already_completed_session(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """If query_upload_status returns total (already done), start a fresh upload."""
        content = b"d" * (MULTIPART_THRESHOLD + 1024)
        lf = make_local_file(name="done.bin", content=content)

        md5 = hashlib.md5(content).hexdigest()
        mock_drive.query_upload_status.return_value = lf.size  # fully completed
        mock_drive.upload_chunk.return_value = UploadResponse(
            file_id="res-id", md5_checksum=md5
        )

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        uploader._session_cache.put(
            "done.bin",
            SessionEntry(
                session_uri="https://upload.example.com/done",
                file_size=lf.size,
                mtime=lf.mtime,
            ),
        )

        result = uploader._upload_resumable(lf, "root-folder-id")
        assert result.success is True
        assert result.resumed is True
        assert result.bytes_uploaded == 0  # no bytes transferred this run
        mock_drive.initiate_resumable_upload.assert_not_called()


# ---------------------------------------------------------------------------
# MD5 verification
# ---------------------------------------------------------------------------


class TestMD5Verification:
    def test_md5_mismatch_trashes_file(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """When MD5 checksums don't match, the uploaded file is trashed."""
        content = b"real content"
        lf = make_local_file(name="mismatch.txt", content=content)

        # Return a wrong checksum
        mock_drive.multipart_upload.return_value = UploadResponse(
            file_id="bad-id", md5_checksum="0000000000000000000000000000dead"
        )

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        from gdrivecopy.uploader import ChecksumError

        with pytest.raises(ChecksumError, match="MD5 mismatch"):
            uploader._upload_small(lf, "root-folder-id")

        mock_drive.trash_file.assert_called_once_with("bad-id")

    def test_checksum_disabled_skips_verification(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """When verify_checksum is False, MD5 mismatch does not cause failure."""
        upload_config.verify_checksum = False
        content = b"some data"
        lf = make_local_file(name="nocheck.txt", content=content)

        mock_drive.multipart_upload.return_value = UploadResponse(
            file_id="ok-id", md5_checksum="wrong-md5-but-ignored"
        )

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        result = uploader._upload_small(lf, "root-folder-id")
        assert result.success is True
        mock_drive.trash_file.assert_not_called()


# ---------------------------------------------------------------------------
# Retry on transient error
# ---------------------------------------------------------------------------


class TestRetryOnTransientError:
    @patch("gdrivecopy.uploader.time.sleep")
    @patch("gdrivecopy.uploader.random.random", return_value=0.5)
    def test_retries_on_rate_limit(
        self,
        _mock_random: MagicMock,
        _mock_sleep: MagicMock,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """RateLimitError triggers a retry with exponential backoff."""
        content = b"retry content"
        lf = make_local_file(name="retry.txt", content=content)

        md5 = hashlib.md5(content).hexdigest()
        # First call: rate limit, second call: success
        mock_drive.multipart_upload.side_effect = [
            RateLimitError(429, "Rate limit"),
            UploadResponse(file_id="ok-id", md5_checksum=md5),
        ]

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        result = uploader._upload_one(lf)
        assert result.success is True
        assert mock_drive.multipart_upload.call_count == 2

    @patch("gdrivecopy.uploader.time.sleep")
    @patch("gdrivecopy.uploader.random.random", return_value=0.5)
    def test_retries_on_500_error(
        self,
        _mock_random: MagicMock,
        _mock_sleep: MagicMock,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """Server errors (5xx) trigger retries."""
        content = b"server error content"
        lf = make_local_file(name="serverr.txt", content=content)

        md5 = hashlib.md5(content).hexdigest()
        mock_drive.multipart_upload.side_effect = [
            DriveApiError(500, "Internal"),
            UploadResponse(file_id="ok-id", md5_checksum=md5),
        ]

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        result = uploader._upload_one(lf)
        assert result.success is True

    def test_permanent_error_does_not_retry(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """A 400 (client error) is permanent and should not be retried."""
        content = b"bad request"
        lf = make_local_file(name="perm.txt", content=content)

        mock_drive.multipart_upload.side_effect = DriveApiError(400, "Bad Request")

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        result = uploader._upload_one(lf)
        assert result.success is False
        assert result.is_permanent is True
        # Should only be called once (no retry)
        assert mock_drive.multipart_upload.call_count == 1

    @patch("gdrivecopy.uploader.time.sleep")
    @patch("gdrivecopy.uploader.random.random", return_value=0.5)
    def test_max_retries_exceeded(
        self,
        _mock_random: MagicMock,
        _mock_sleep: MagicMock,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """After MAX_RETRIES transient failures, the file is marked failed."""
        content = b"exhausted"
        lf = make_local_file(name="exhaust.txt", content=content)

        mock_drive.multipart_upload.side_effect = RateLimitError(429, "Rate limit")

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        result = uploader._upload_one(lf)
        assert result.success is False
        assert result.error == "max retries exceeded"


# ---------------------------------------------------------------------------
# Daily limit handling
# ---------------------------------------------------------------------------


class TestDailyLimitHandling:
    def test_daily_limit_propagates(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """DailyLimitError is re-raised so the pool can stop all workers."""
        content = b"limit hit"
        lf = make_local_file(name="limit.txt", content=content)

        mock_drive.multipart_upload.side_effect = DailyLimitError(403, "Daily limit")

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        with pytest.raises(DailyLimitError):
            uploader._upload_one(lf)

    def test_daily_limit_event_stops_new_uploads(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """Once the daily_limit_hit event is set, new uploads raise immediately."""
        content = b"blocked"
        lf = make_local_file(name="blocked.txt", content=content)

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}
        uploader._daily_limit_hit.set()

        with pytest.raises(DailyLimitError):
            uploader._upload_one(lf)

        # multipart_upload should never be called because we bail early
        mock_drive.multipart_upload.assert_not_called()


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_success_resets_counter(self) -> None:
        """record_success() resets the consecutive failure count to zero."""
        breaker = _CircuitBreaker(threshold=3, pause_seconds=0)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        # Should not pause on next failure since counter was reset
        breaker.record_failure()
        # If we get here without sleeping, the test passes

    @patch("gdrivecopy.uploader.time.sleep")
    def test_every_failure_triggers_pause(self, mock_sleep: MagicMock) -> None:
        """The breaker only sleeps when the consecutive failure threshold is reached."""
        breaker = _CircuitBreaker(threshold=3, pause_seconds=30)

        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()

        # The breaker sleeps once when threshold (3) is reached on the 3rd failure.
        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(30)

    @patch("gdrivecopy.uploader.time.sleep")
    def test_threshold_resets_counter(self, mock_sleep: MagicMock) -> None:
        """At the threshold the counter resets, so a warning is logged once per cycle."""
        breaker = _CircuitBreaker(threshold=2, pause_seconds=10)

        breaker.record_failure()
        breaker.record_failure()  # threshold hit, counter resets to 0, sleeps
        breaker.record_success()  # explicit reset (already 0)
        breaker.record_failure()  # counter = 1 again, no threshold hit yet

        # Only 1 sleep: when the threshold was hit on the 2nd failure
        assert mock_sleep.call_count == 1


# ---------------------------------------------------------------------------
# Folder creation
# ---------------------------------------------------------------------------


class TestEnsureParentFolder:
    def test_root_level_file(
        self, upload_config: UploadConfig, mock_drive: MagicMock
    ) -> None:
        """A file at the root level returns the drive_folder_id directly."""
        uploader = Uploader(upload_config, mock_drive)
        uploader._folder_map = {"": "root-folder-id"}

        parent_id = uploader._ensure_parent_folder("file.txt")
        assert parent_id == "root-folder-id"
        mock_drive.create_folder.assert_not_called()

    def test_nested_file_creates_folders(
        self, upload_config: UploadConfig, mock_drive: MagicMock
    ) -> None:
        """For 'a/b/file.txt', both 'a/' and 'a/b/' folders are created."""
        mock_drive.create_folder.side_effect = ["folder-a-id", "folder-b-id"]

        uploader = Uploader(upload_config, mock_drive)
        uploader._folder_map = {"": "root-folder-id"}

        parent_id = uploader._ensure_parent_folder("a/b/file.txt")
        assert parent_id == "folder-b-id"
        assert mock_drive.create_folder.call_count == 2

    def test_existing_folder_is_reused(
        self, upload_config: UploadConfig, mock_drive: MagicMock
    ) -> None:
        """If the folder already exists in the map, it is not re-created."""
        uploader = Uploader(upload_config, mock_drive)
        uploader._folder_map = {"": "root-folder-id", "photos/": "photos-id"}

        parent_id = uploader._ensure_parent_folder("photos/img.jpg")
        assert parent_id == "photos-id"
        mock_drive.create_folder.assert_not_called()


# ---------------------------------------------------------------------------
# Full run (integration-style with mocks)
# ---------------------------------------------------------------------------


class TestUploaderRun:
    @patch("gdrivecopy.uploader.time.sleep")
    def test_dry_run_skips_uploads(
        self,
        _mock_sleep: MagicMock,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        source_tree: Path,
    ) -> None:
        """In dry_run mode, files are scanned but never uploaded."""
        upload_config.dry_run = True

        uploader = Uploader(upload_config, mock_drive)
        stats = uploader.run()

        assert stats.files_scanned == 3
        assert stats.files_uploaded == 0
        mock_drive.multipart_upload.assert_not_called()
        mock_drive.initiate_resumable_upload.assert_not_called()

    @patch("gdrivecopy.uploader.time.sleep")
    def test_all_files_already_on_drive(
        self,
        _mock_sleep: MagicMock,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        source_tree: Path,
    ) -> None:
        """When all files are already on Drive with matching sizes, all are skipped."""
        mock_drive.list_all.return_value = (
            {
                "file_a.txt": DriveFile(id="d1", name="file_a.txt", size=11),
                "subdir/file_b.bin": DriveFile(id="d2", name="file_b.bin", size=5),
                "subdir/nested/file_c.dat": DriveFile(id="d3", name="file_c.dat", size=3),
            },
            {"": "root-folder-id"},
        )

        uploader = Uploader(upload_config, mock_drive)
        stats = uploader.run()

        assert stats.files_scanned == 3
        assert stats.files_skipped == 3
        assert stats.files_uploaded == 0
