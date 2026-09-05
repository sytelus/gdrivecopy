"""Tests for gdrivecopy.uploader -- upload orchestration."""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from gdrivecopy.drive import (
    DriveApiError,
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
)
from gdrivecopy.scanner import ScanResult
from gdrivecopy.uploader import (
    MULTIPART_THRESHOLD,
    Uploader,
    _BandwidthLimiter,
    _CircuitBreaker,
)

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
        uploader._file_map = {"existing.txt": DriveFile(id="d1", name="existing.txt", size=100)}

        lf = LocalFile(
            path=Path("/fake/existing.txt"),
            relative_path="existing.txt",
            size=100,
            mtime="2025-01-01T00:00:00+00:00",
        )
        assert uploader._classify(lf) == "skip"

    def test_visible_drive_file_clears_obsolete_session(
        self, upload_config: UploadConfig, mock_drive: MagicMock
    ) -> None:
        """A completed item visible in Drive makes its cached upload URI obsolete."""
        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {"existing.txt": DriveFile(id="d1", name="existing.txt", size=100)}
        uploader._session_cache.put(
            "existing.txt",
            SessionEntry(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=old",
                100,
                "2025-01-01T00:00:00+00:00",
            ),
        )
        lf = LocalFile(
            path=Path("/fake/existing.txt"),
            relative_path="existing.txt",
            size=100,
            mtime="2025-01-01T00:00:00+00:00",
        )

        assert uploader._classify(lf) == "skip"
        assert uploader._session_cache.get("existing.txt") is None

    def test_dry_run_does_not_mutate_obsolete_session(
        self, upload_config: UploadConfig, mock_drive: MagicMock
    ) -> None:
        """Classification during a dry run is read-only for the session cache."""
        upload_config.dry_run = True
        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {"existing.txt": DriveFile(id="d1", name="existing.txt", size=100)}
        entry = SessionEntry(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=old",
            100,
            "2025-01-01T00:00:00+00:00",
        )
        uploader._session_cache.put("existing.txt", entry)
        lf = LocalFile(
            path=Path("/fake/existing.txt"),
            relative_path="existing.txt",
            size=100,
            mtime="2025-01-01T00:00:00+00:00",
        )

        assert uploader._classify(lf) == "skip"
        assert uploader._session_cache.get("existing.txt") == entry

    def test_file_exists_different_size_is_mismatch(
        self, upload_config: UploadConfig, mock_drive: MagicMock
    ) -> None:
        """A file present on Drive with different size is a size_mismatch."""
        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {"changed.txt": DriveFile(id="d1", name="changed.txt", size=50)}

        lf = LocalFile(
            path=Path("/fake/changed.txt"),
            relative_path="changed.txt",
            size=100,
            mtime="2025-01-01T00:00:00+00:00",
        )
        result = uploader._classify(lf)
        assert result == "size_mismatch"
        assert len(uploader._stats.mismatch_details) == 1

    def test_file_with_unknown_drive_size_is_mismatch(
        self, upload_config: UploadConfig, mock_drive: MagicMock
    ) -> None:
        """A native Drive item must not cause an empty local file to be skipped."""
        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {"native": DriveFile(id="d1", name="native", size=None)}
        lf = LocalFile(
            path=Path("/fake/native"),
            relative_path="native",
            size=0,
            mtime="2025-01-01T00:00:00+00:00",
        )

        assert uploader._classify(lf) == "size_mismatch"
        assert "drive=unknown" in uploader._stats.mismatch_details[0]


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
        mock_drive.multipart_upload.return_value = UploadResponse(file_id="mp-id", md5_checksum=md5)

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
        mock_drive.upload_chunk.return_value = UploadResponse(file_id="res-id", md5_checksum=md5)

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
        mock_drive.query_upload_status.return_value = UploadStatus(confirmed_bytes=512)
        mock_drive.upload_chunk.return_value = UploadResponse(file_id="res-id", md5_checksum=md5)

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        # Pre-populate the session cache
        uploader._session_cache.put(
            "resume.bin",
            SessionEntry(
                session_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=resume-session",
                file_size=lf.size,
                mtime=lf.mtime,
                source_path=str(lf.path),
                parent_id="root-folder-id",
                mtime_ns=lf.mtime_ns,
            ),
        )

        result = uploader._upload_resumable(lf, "root-folder-id")
        assert result.success is True
        assert result.resumed is True
        assert result.bytes_uploaded == lf.size - 512
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
        mock_drive.upload_chunk.return_value = UploadResponse(file_id="res-id", md5_checksum=md5)

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        # Session has a different file_size -- triggers stale detection
        uploader._session_cache.put(
            "stale.bin",
            SessionEntry(
                session_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=stale",
                file_size=lf.size + 999,  # different size
                mtime=lf.mtime,
                source_path=str(lf.path),
                parent_id="root-folder-id",
                mtime_ns=lf.mtime_ns,
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
        mock_drive.query_upload_status.side_effect = UploadSessionError(404, "Not Found")
        mock_drive.upload_chunk.return_value = UploadResponse(file_id="new-id", md5_checksum=md5)

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        uploader._session_cache.put(
            "expired.bin",
            SessionEntry(
                session_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=expired",
                file_size=lf.size,
                mtime=lf.mtime,
                source_path=str(lf.path),
                parent_id="root-folder-id",
                mtime_ns=lf.mtime_ns,
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
        """A completed cached session is checksum-verified without uploading again."""
        content = b"d" * (MULTIPART_THRESHOLD + 1024)
        lf = make_local_file(name="done.bin", content=content)

        md5 = hashlib.md5(content).hexdigest()
        mock_drive.query_upload_status.return_value = UploadStatus(
            confirmed_bytes=lf.size,
            completed=UploadResponse(file_id="res-id", md5_checksum=md5),
        )
        mock_drive.upload_chunk.return_value = UploadResponse(file_id="res-id", md5_checksum=md5)

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        uploader._session_cache.put(
            "done.bin",
            SessionEntry(
                session_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=done",
                file_size=lf.size,
                mtime=lf.mtime,
                source_path=str(lf.path),
                parent_id="root-folder-id",
                mtime_ns=lf.mtime_ns,
            ),
        )

        result = uploader._upload_resumable(lf, "root-folder-id")
        assert result.success is True
        assert result.resumed is True
        assert result.bytes_uploaded == 0  # no bytes transferred this run
        mock_drive.initiate_resumable_upload.assert_not_called()

    def test_transient_status_error_preserves_session(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """A temporary status-query failure must not discard a valid session."""
        lf = make_local_file(name="keep.bin", content=b"x")
        entry = SessionEntry(
            session_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=keep",
            file_size=lf.size,
            mtime=lf.mtime,
            source_path=str(lf.path),
            parent_id="root-folder-id",
            mtime_ns=lf.mtime_ns,
        )
        mock_drive.query_upload_status.side_effect = RateLimitError(429, "slow down")
        uploader = Uploader(upload_config, mock_drive)
        uploader._session_cache.put(lf.relative_path, entry)

        with pytest.raises(RateLimitError):
            uploader._try_resume(lf, "root-folder-id")

        assert uploader._session_cache.get(lf.relative_path) == entry


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

        with patch.object(Uploader, "_file_md5", side_effect=AssertionError("unexpected hash")):
            result = uploader._upload_small(lf, "root-folder-id")
        assert result.success is True
        mock_drive.trash_file.assert_not_called()

    def test_missing_drive_checksum_trashes_file(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """Enabled verification cannot silently pass when Drive omits its checksum."""
        lf = make_local_file(name="missing-md5.txt", content=b"content")
        mock_drive.multipart_upload.return_value = UploadResponse("unverified-id", None)
        uploader = Uploader(upload_config, mock_drive)

        from gdrivecopy.uploader import ChecksumError

        with pytest.raises(ChecksumError, match="omitted"):
            uploader._upload_small(lf, "root-folder-id")

        mock_drive.trash_file.assert_called_once_with("unverified-id")

    def test_cleanup_failure_stops_duplicate_retry(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """An untrashable bad upload is reported instead of uploaded a second time."""
        lf = make_local_file(name="unsafe.txt", content=b"content")
        mock_drive.multipart_upload.return_value = UploadResponse("unsafe-id", "wrong")
        mock_drive.trash_file.side_effect = DriveApiError(500, "trash failed")
        uploader = Uploader(upload_config, mock_drive)

        result = uploader._upload_one(lf)

        assert result.success is False
        assert result.is_permanent is True
        assert "could not trash" in (result.error or "")
        mock_drive.multipart_upload.assert_called_once()


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

    def test_ambiguous_multipart_500_stops_without_retry(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """A 5xx after one-request upload may conceal success, so it is not retried."""
        content = b"server error content"
        lf = make_local_file(name="serverr.txt", content=content)

        mock_drive.multipart_upload.side_effect = DriveApiError(500, "Internal")

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        result = uploader._upload_one(lf)
        assert result.success is False
        assert result.is_permanent is True
        assert "rerun to reconcile" in (result.error or "")
        mock_drive.multipart_upload.assert_called_once()

    def test_ambiguous_multipart_timeout_stops_without_retry(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """A lost multipart response is reconciled by the next run, not duplicated now."""
        content = b"possibly uploaded"
        lf = make_local_file(name="timeout.txt", content=content)
        mock_drive.multipart_upload.side_effect = requests.Timeout("timed out")
        uploader = Uploader(upload_config, mock_drive)

        result = uploader._upload_one(lf)

        assert result.success is False
        assert result.is_permanent is True
        assert "response was lost" in (result.error or "")
        mock_drive.multipart_upload.assert_called_once()

    @pytest.mark.parametrize("status_code", [408, 500])
    @patch("gdrivecopy.uploader.time.sleep")
    def test_retries_resumable_initiation_error(
        self,
        _mock_sleep: MagicMock,
        status_code: int,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """Transient initiation errors safely retry because no file data was sent."""
        content = b"r" * (MULTIPART_THRESHOLD + 1)
        lf = make_local_file(name="retry-init.bin", content=content)
        upload_config.chunk_size = 9 * 1024 * 1024
        mock_drive.initiate_resumable_upload.side_effect = [
            DriveApiError(status_code, "temporary"),
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=new-session",
        ]
        mock_drive.upload_chunk.return_value = UploadResponse(
            "ok-id", hashlib.md5(content).hexdigest()
        )
        uploader = Uploader(upload_config, mock_drive)

        result = uploader._upload_one(lf)

        assert result.success is True
        assert mock_drive.initiate_resumable_upload.call_count == 2

    @patch("gdrivecopy.uploader.time.sleep")
    def test_restarts_rejected_active_session(
        self,
        _mock_sleep: MagicMock,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """An invalid active session is discarded before a fresh session is created."""
        content = b"s" * (MULTIPART_THRESHOLD + 1)
        lf = make_local_file(name="restart-session.bin", content=content)
        upload_config.chunk_size = 9 * 1024 * 1024
        mock_drive.initiate_resumable_upload.side_effect = [
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=old-session",
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=new-session",
        ]
        mock_drive.upload_chunk.side_effect = [
            UploadSessionError(404, "expired"),
            UploadResponse("ok-id", hashlib.md5(content).hexdigest()),
        ]
        uploader = Uploader(upload_config, mock_drive)

        result = uploader._upload_one(lf)

        assert result.success is True
        assert mock_drive.initiate_resumable_upload.call_count == 2
        assert uploader._session_cache.get(lf.relative_path) is None

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
        """Exhausting the configured retry budget marks the file failed."""
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
# Blocking quota handling
# ---------------------------------------------------------------------------


class TestQuotaLimitHandling:
    def test_quota_limit_propagates(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """QuotaLimitError is re-raised so the pool can stop all workers."""
        content = b"limit hit"
        lf = make_local_file(name="limit.txt", content=content)

        mock_drive.multipart_upload.side_effect = QuotaLimitError(403, "Quota limit")

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}

        with pytest.raises(QuotaLimitError):
            uploader._upload_one(lf)

    def test_quota_limit_event_stops_new_uploads(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """Once the blocking quota event is set, new uploads raise immediately."""
        content = b"blocked"
        lf = make_local_file(name="blocked.txt", content=content)

        uploader = Uploader(upload_config, mock_drive)
        uploader._file_map = {}
        uploader._folder_map = {"": "root-folder-id"}
        uploader._quota_limit_hit.set()

        with pytest.raises(QuotaLimitError):
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


class TestBandwidthLimiter:
    @patch("gdrivecopy.uploader.time.sleep")
    @patch("gdrivecopy.uploader.time.monotonic", return_value=0.0)
    def test_workers_share_one_transfer_schedule(
        self, _mock_monotonic: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """Successive workers reserve non-overlapping bandwidth slots."""
        limiter = _BandwidthLimiter(bytes_per_second=10)

        limiter.wait_for_slot(10)
        limiter.wait_for_slot(10)

        mock_sleep.assert_called_once_with(1.0)


class TestSourceMutation:
    def test_changed_before_upload_is_permanent_failure(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """A file changed after scanning is left for the next run."""
        lf = make_local_file(name="changing.txt", content=b"before")
        lf.path.write_bytes(b"after and larger")
        uploader = Uploader(upload_config, mock_drive)

        result = uploader._upload_one(lf)

        assert result.success is False
        assert result.is_permanent is True
        assert "changed during upload" in (result.error or "")
        mock_drive.multipart_upload.assert_not_called()

    def test_changed_during_small_upload_is_trashed(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """A raced multipart upload is removed rather than accepted by stale metadata."""
        content = b"before"
        lf = make_local_file(name="race.txt", content=content)

        def _mutating_upload(**_kwargs: Any) -> UploadResponse:
            lf.path.write_bytes(b"after and larger")
            return UploadResponse("raced-id", hashlib.md5(content).hexdigest())

        mock_drive.multipart_upload.side_effect = _mutating_upload
        uploader = Uploader(upload_config, mock_drive)

        result = uploader._upload_one(lf)

        assert result.success is False
        assert result.is_permanent is True
        mock_drive.trash_file.assert_called_once_with("raced-id")

    def test_deleted_during_small_upload_is_trashed(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """A vanished source cannot leave its newly uploaded Drive item accepted."""
        content = b"before"
        lf = make_local_file(name="vanished.txt", content=content)

        def _deleting_upload(**_kwargs: Any) -> UploadResponse:
            lf.path.unlink()
            return UploadResponse("vanished-id", hashlib.md5(content).hexdigest())

        mock_drive.multipart_upload.side_effect = _deleting_upload
        uploader = Uploader(upload_config, mock_drive)

        result = uploader._upload_one(lf)

        assert result.success is False
        assert result.is_permanent is True
        assert "Cannot re-inspect" in (result.error or "")
        mock_drive.trash_file.assert_called_once_with("vanished-id")

    def test_growth_during_resumable_upload_never_exceeds_declared_size(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """A growing source cannot make the final chunk exceed Content-Range total."""
        content = b"g" * (MULTIPART_THRESHOLD + 1)
        lf = make_local_file(name="growing.bin", content=content)
        upload_config.chunk_size = 4 * 1024 * 1024
        calls = 0

        def _upload_chunk(**kwargs: Any) -> UploadResponse | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                with lf.path.open("ab") as file_obj:
                    file_obj.write(b"growth")
            end = kwargs["start"] + len(kwargs["data"])
            assert end <= kwargs["total"]
            if end == kwargs["total"]:
                return UploadResponse("grown-id", hashlib.md5(content).hexdigest())
            return None

        mock_drive.upload_chunk.side_effect = _upload_chunk
        uploader = Uploader(upload_config, mock_drive)

        result = uploader._upload_one(lf)

        assert result.success is False
        assert result.is_permanent is True
        assert mock_drive.upload_chunk.call_args.kwargs["data"] == b"g"
        mock_drive.trash_file.assert_called_once_with("grown-id")

    def test_iso_mtime_fallback_detects_change(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """Callers without nanosecond metadata still receive mutation protection."""
        scanned = make_local_file(name="portable.txt", content=b"same size")
        lf = replace(scanned, mtime_ns=None)
        os.utime(lf.path, (lf.path.stat().st_atime, lf.path.stat().st_mtime + 10))
        uploader = Uploader(upload_config, mock_drive)

        result = uploader._upload_one(lf)

        assert result.success is False
        assert "changed during upload" in (result.error or "")

    def test_file_shortened_after_resume_check_fails_permanently(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        make_local_file: Any,
    ) -> None:
        """An early EOF while hashing a confirmed prefix is a source mutation."""
        content = b"x" * (MULTIPART_THRESHOLD + 1)
        lf = make_local_file(name="shortened.bin", content=content)
        uploader = Uploader(upload_config, mock_drive)
        uploader._session_cache.put(
            lf.relative_path,
            SessionEntry(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=session",
                lf.size,
                lf.mtime,
                source_path=str(lf.path),
                parent_id="root-folder-id",
                mtime_ns=lf.mtime_ns,
            ),
        )
        mock_drive.query_upload_status.return_value = UploadStatus(confirmed_bytes=512)

        with patch("builtins.open", return_value=io.BytesIO(b"too short")):
            result = uploader._upload_one(lf)

        assert result.success is False
        assert result.is_permanent is True
        assert "became shorter" in (result.error or "")
        mock_drive.upload_chunk.assert_not_called()


class TestConfigValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("drive_folder_id", ""),
            ("transfers", 0),
            ("chunk_size", 1),
            ("bwlimit", 0),
        ],
    )
    def test_invalid_library_config_rejected(
        self,
        field: str,
        value: int | str,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
    ) -> None:
        """Library callers receive early errors for unsafe numeric settings."""
        setattr(upload_config, field, value)

        with pytest.raises(ValueError):
            Uploader(upload_config, mock_drive)


# ---------------------------------------------------------------------------
# Folder creation
# ---------------------------------------------------------------------------


class TestEnsureParentFolder:
    def test_root_level_file(self, upload_config: UploadConfig, mock_drive: MagicMock) -> None:
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
        assert stats.files_to_upload == 3
        assert stats.files_uploaded == 0
        mock_drive.multipart_upload.assert_not_called()
        mock_drive.initiate_resumable_upload.assert_not_called()

    def test_reused_uploader_resets_per_run_control_state(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
    ) -> None:
        """A second library run does not inherit throttling or breaker state."""
        upload_config.dry_run = True
        uploader = Uploader(upload_config, mock_drive)
        uploader._breaker._consecutive_failures = 7
        uploader._bandwidth_limiter._next_slot = float("inf")

        uploader.run()

        assert uploader._breaker._consecutive_failures == 0
        assert uploader._bandwidth_limiter._next_slot == 0.0

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
        assert stats.files_to_upload == 0
        assert stats.files_uploaded == 0

    def test_tool_owned_files_inside_source_are_excluded(
        self,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
        source_tree: Path,
    ) -> None:
        """Credentials and mutable runtime artifacts can never become payload."""
        runtime_names = [
            "credentials.json",
            "token.json",
            "token.json.tmp",
            "sessions.json",
            "report.json",
            "gdrivecopy_active.log",
        ]
        for name in runtime_names:
            (source_tree / name).write_text("{}", encoding="utf-8")

        upload_config.dry_run = True
        upload_config.credentials_path = source_tree / "credentials.json"
        upload_config.token_path = source_tree / "token.json"
        upload_config.session_path = source_tree / "sessions.json"
        upload_config.log_dir = source_tree
        upload_config.log_path = source_tree / "gdrivecopy_active.log"

        stats = Uploader(upload_config, mock_drive).run()

        assert stats.files_excluded == len(runtime_names)
        assert stats.files_scanned == 3
        assert stats.files_to_upload == 3

    @patch("gdrivecopy.uploader.scan_local")
    def test_scan_errors_are_reported(
        self,
        mock_scan: MagicMock,
        upload_config: UploadConfig,
        mock_drive: MagicMock,
    ) -> None:
        """Unreadable source entries make the final result visibly incomplete."""
        mock_scan.return_value = ScanResult(
            files=[],
            symlinks_skipped=0,
            errors=["Cannot scan secret: denied"],
            files_excluded=2,
        )
        upload_config.dry_run = True

        stats = Uploader(upload_config, mock_drive).run()

        assert stats.scan_errors == 1
        assert stats.files_excluded == 2
        assert stats.errors == ["Cannot scan secret: denied"]
