"""Regression tests for destination identity and verification failure boundaries."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from gdrivecopy.drive import DriveFile, UploadResponse, UploadStatus
from gdrivecopy.models import SessionEntry
from gdrivecopy.uploader import Uploader


@pytest.mark.parametrize("collision", ["file_at_parent", "folder_at_file"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_local_drive_type_conflicts_never_upload(upload_config, mock_drive, collision, dry_run):
    upload_config.dry_run = dry_run
    upload_config.verify_checksum = False
    if collision == "file_at_parent":
        mock_drive.list_all.return_value = (
            {"subdir": DriveFile("existing", "subdir", 1)},
            {"": "root-folder-id"},
        )
    else:
        mock_drive.list_all.return_value = (
            {},
            {"": "root-folder-id", "file_a.txt/": "existing-folder"},
        )
    stats = Uploader(upload_config, mock_drive).run()
    assert stats.path_conflicts == (2 if collision == "file_at_parent" else 1)
    uploaded_names = [call.kwargs["name"] for call in mock_drive.multipart_upload.call_args_list]
    assert (
        ("file_b.bin" not in uploaded_names)
        if collision == "file_at_parent"
        else ("file_a.txt" not in uploaded_names)
    )


def test_completed_resume_read_error_trashes_unverified_item(
    upload_config, mock_drive, make_local_file
):
    lf = make_local_file()
    completed = UploadStatus(lf.size, UploadResponse("new-item", "checksum"))
    uploader = Uploader(upload_config, mock_drive)
    with (
        patch.object(uploader, "_try_resume", return_value=(None, completed, True)),
        patch.object(uploader, "_file_md5", side_effect=PermissionError("source unreadable")),
        pytest.raises(OSError),
    ):
        uploader._upload_resumable(lf, "root-folder-id")
    mock_drive.trash_file.assert_called_once_with("new-item")


def test_completed_resume_without_verification_does_not_hash(
    upload_config, mock_drive, make_local_file
):
    upload_config.verify_checksum = False
    lf = make_local_file()
    completed = UploadStatus(lf.size, UploadResponse("new-item", None))
    uploader = Uploader(upload_config, mock_drive)
    with (
        patch.object(uploader, "_try_resume", return_value=(None, completed, True)),
        patch.object(uploader, "_file_md5", side_effect=AssertionError("unexpected read")),
    ):
        assert uploader._upload_resumable(lf, "root-folder-id").success


def test_old_session_without_source_and_destination_identity_is_discarded(
    upload_config, mock_drive, make_local_file
):
    lf = make_local_file()
    uploader = Uploader(upload_config, mock_drive)
    uploader._session_cache.put(
        lf.relative_path,
        SessionEntry(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=old", lf.size, lf.mtime
        ),
    )
    uri, _, resumed = uploader._try_resume(lf, "root-folder-id")
    assert uri is None and not resumed
    mock_drive.query_upload_status.assert_not_called()


@pytest.mark.parametrize("change", ["source", "destination", "mtime_ns"])
def test_sessions_cannot_cross_upload_identity(upload_config, mock_drive, make_local_file, change):
    lf = make_local_file()
    uploader = Uploader(upload_config, mock_drive)
    entry = SessionEntry(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=scoped",
        lf.size,
        lf.mtime,
        source_path=str(lf.path),
        parent_id="root-folder-id",
        mtime_ns=lf.mtime_ns,
    )
    if change == "source":
        entry = replace(entry, source_path=str(lf.path.parent / "other" / lf.path.name))
    elif change == "destination":
        entry = replace(entry, parent_id="other-folder-id")
    else:
        entry = replace(entry, mtime_ns=lf.mtime_ns + 1)
    uploader._session_cache.put(lf.relative_path, entry)
    uri, _, resumed = uploader._try_resume(lf, "root-folder-id")
    assert uri is None and not resumed
    mock_drive.query_upload_status.assert_not_called()


def test_no_session_remove_does_not_write_cache(upload_config, mock_drive):
    uploader = Uploader(upload_config, mock_drive)
    uploader._session_cache.remove("absent")
    assert not upload_config.session_path.exists()


def test_cache_write_error_does_not_bypass_checksum_cleanup(
    upload_config, mock_drive, make_local_file
):
    lf = make_local_file()
    uploader = Uploader(upload_config, mock_drive)
    mock_drive.upload_chunk.return_value = UploadResponse("bad-item", "bad-checksum")
    with patch.object(uploader._session_cache, "save", side_effect=OSError("disk full")):
        from gdrivecopy.uploader import ChecksumError

        with pytest.raises(ChecksumError):
            uploader._upload_resumable(lf, "root-folder-id")
    mock_drive.trash_file.assert_called_once_with("bad-item")


def test_scoped_session_survives_process_restart(upload_config, mock_drive, make_local_file):
    lf = make_local_file()
    first = Uploader(upload_config, mock_drive)
    entry = SessionEntry(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=scoped",
        lf.size,
        lf.mtime,
        source_path=str(lf.path),
        parent_id="root-folder-id",
        mtime_ns=lf.mtime_ns,
    )
    first._session_cache.put(lf.relative_path, entry)
    second = Uploader(upload_config, mock_drive)
    second._session_cache.load()
    uri, _, resumed = second._try_resume(lf, "root-folder-id")
    assert uri == entry.session_uri and resumed
    mock_drive.query_upload_status.assert_called_once_with(entry.session_uri, lf.size)


def test_replacement_preserving_size_and_mtime_is_rejected(upload_config, mock_drive):
    import os

    from gdrivecopy.scanner import scan_local

    lf = scan_local(upload_config.source_dir).files[0]
    replacement = lf.path.with_suffix(".replacement")
    replacement.write_bytes(b"x" * lf.size)
    os.utime(replacement, ns=(lf.mtime_ns, lf.mtime_ns))
    replacement.replace(lf.path)
    result = Uploader(upload_config, mock_drive)._upload_one(lf)
    assert not result.success and result.is_permanent
    mock_drive.multipart_upload.assert_not_called()


def test_quota_stop_signals_workers_and_report(upload_config, mock_drive):
    from gdrivecopy.drive import QuotaLimitError

    mock_drive.multipart_upload.side_effect = QuotaLimitError(403, "storage full")
    uploader = Uploader(upload_config, mock_drive)
    stats = uploader.run()
    assert uploader._quota_limit_hit.is_set()
    assert stats.quota_limit_hits == 1
    assert mock_drive.multipart_upload.call_count == 1


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_unauthorized_upload_has_one_refresh_budget(
    upload_config, mock_drive, make_local_file, refresh_fails
):
    from gdrivecopy.drive import DriveApiError

    lf = make_local_file()
    mock_drive.multipart_upload.side_effect = DriveApiError(401, "unauthorized")
    if refresh_fails:
        mock_drive.refresh_credentials.side_effect = RuntimeError("refresh rejected")
    result = Uploader(upload_config, mock_drive)._upload_one(lf)
    assert not result.success and result.is_permanent
    assert mock_drive.multipart_upload.call_count == (1 if refresh_fails else 2)
    mock_drive.refresh_credentials.assert_called_once()


def test_quota_drains_inflight_success_without_submitting_more(upload_config, mock_drive):
    import threading

    from gdrivecopy.drive import QuotaLimitError
    from gdrivecopy.scanner import scan_local
    from gdrivecopy.uploader import _WorkerResult

    upload_config.transfers = 2
    uploader = Uploader(upload_config, mock_drive)
    files = scan_local(upload_config.source_dir).files
    second_started = threading.Event()

    def worker(lf):
        if lf == files[0]:
            assert second_started.wait(5), "second worker did not start"
            raise QuotaLimitError(403, "quota")
        assert lf == files[1], "work was submitted after the quota stop"
        second_started.set()
        assert uploader._quota_limit_hit.wait(5), "quota was not propagated"
        return _WorkerResult(success=True, bytes_uploaded=lf.size)

    with patch.object(uploader, "_upload_one", side_effect=worker) as upload:
        uploader._upload_all(files)
    assert upload.call_count == 2
    assert uploader._stats.quota_limit_hits == 1
    assert uploader._stats.files_uploaded == 1
    assert uploader._stats.bytes_uploaded == files[1].size
