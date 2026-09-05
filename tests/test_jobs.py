"""End-to-end transfer, cancellation, and crash-recovery invariants."""

import json
import os
from unittest.mock import patch

import pytest

from gdrivecopy.control import Cancelled, ProgressModel
from gdrivecopy.downloader import Downloader
from gdrivecopy.drive import DriveApiError, QuotaLimitError, RateLimitError
from gdrivecopy.jobstore import JobLock, JobStore
from gdrivecopy.transfer import TransferRunner
from tests.fake_drive import FakeDrive


@pytest.fixture
def job(tmp_path):
    def create(direction="download", **options):
        local = tmp_path / "local"
        if direction == "upload":
            local.mkdir(exist_ok=True)
        config = {
            "job_id": "20260904-120000-1234abcd",
            "direction": direction,
            "local_path": str(local),
            "source": "source",
            "destination": "destination",
            "remote_root": "root",
            "state_dir": str(tmp_path / "state"),
            "account": "test",
            "account_id": "user",
            "account_email": "test@example.test",
            "transfers": 1,
            "chunk_size": 256 * 1024,
            "bwlimit": None,
            "retries": 3,
            "verification": "checksum",
            "existing": "size",
            "patterns": [],
            "exclude": [],
            "dry_run": False,
            **options,
        }
        store = JobStore(tmp_path / "state" / "job")
        store.set("config", config)
        return store, config, local

    return create


def test_download_verifies_before_publishing(job):
    store, config, local = job()
    drive = FakeDrive()
    content = b"abc" * 200_000
    drive.add_file("video.bin", content)
    report = TransferRunner(store, drive, config).run()
    assert report["status"] == "complete"
    assert report["counts"]["copied"]["files"] == 1
    assert (local / "video.bin").read_bytes() == content
    assert not list(local.glob("*.part"))
    store.close()


def test_download_cancel_resume_preserves_confirmed_ranges(job):
    store, config, local = job()
    drive = FakeDrive()
    content = b"x" * 700_000
    drive.add_file("big.bin", content)
    drive.cancel_after_range = True
    first = TransferRunner(store, drive, config).run()
    assert first["status"] == "cancelled"
    assert not (local / "big.bin").exists()
    assert store.file("big.bin")["offset"] == config["chunk_size"]
    directory = store.directory
    store.close()
    resumed = JobStore(directory)
    second = TransferRunner(resumed, drive, config).run()
    assert second["status"] == "complete"
    assert second["resumed_bytes_this_run"] == config["chunk_size"]
    assert drive.download_calls[1][1] == config["chunk_size"]
    assert (local / "big.bin").read_bytes() == content
    resumed.close()


def test_corrupt_download_never_becomes_final_file(job):
    store, config, local = job()
    drive = FakeDrive()
    identity = drive.add_file("wrong.bin", b"correct")
    drive.content[identity] = b"corrupt"
    report = TransferRunner(store, drive, config).run()
    assert report["status"] == "incomplete"
    assert not (local / "wrong.bin").exists()
    assert store.file("wrong.bin")["offset"] == 0
    store.close()


def test_multipart_lost_ack_uses_same_durable_id(job):
    store, config, local = job("upload")
    (local / "small.txt").write_text("hello")
    drive = FakeDrive()
    drive.lose_multipart_response = True
    runner = TransferRunner(store, drive, config)
    with patch.object(runner.control, "wait"):
        report = runner.run()
    assert report["status"] == "complete"
    assert len(drive.content) == 1
    assert drive.upload_calls == 2
    assert store.file("small.txt")["remote_id"] in drive.content
    store.close()


def test_unexpected_upload_identity_is_not_accepted(job):
    from gdrivecopy.drive import UploadResponse

    store, config, local = job("upload")
    (local / "small").write_bytes(b"data")
    drive = FakeDrive()
    original = drive.multipart_upload

    def wrong_identity(*args, **kwargs):
        response = original(*args, **kwargs)
        return UploadResponse("unrelated-id", response.md5_checksum)

    drive.multipart_upload = wrong_identity
    assert TransferRunner(store, drive, config).run()["status"] == "incomplete"
    assert store.file("small")["status"] == "failed"
    store.close()


def test_completed_upload_edited_before_resume_is_never_trashed(job):
    store, config, local = job("upload")
    (local / "file").write_bytes(b"original")
    drive = FakeDrive()
    assert TransferRunner(store, drive, config).run()["status"] == "complete"
    identity = store.file("file")["remote_id"]
    drive.add_file("file", b"modified", identity=identity)
    uploads = drive.upload_calls
    with patch.object(drive, "trash_file") as trash:
        report = TransferRunner(store, drive, config).run()
        assert TransferRunner(store, drive, config).run()["status"] == "incomplete"
    assert report["status"] == "incomplete"
    assert report["counts"]["conflict"]["files"] == 1
    assert "content changed" in store.file("file")["error"]
    assert drive.content[identity] == b"modified"
    assert drive.upload_calls == uploads
    trash.assert_not_called()
    store.close()


def test_resumable_upload_survives_new_runner_and_database_connection(job):
    store, config, local = job("upload")
    (local / "large.bin").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    drive = FakeDrive()
    drive.cancel_after_chunk = True
    first = TransferRunner(store, drive, config).run()
    assert first["status"] == "cancelled"
    session = store.file("large.bin")["session"]
    assert session
    directory = store.directory
    store.close()
    resumed = JobStore(directory)
    second = TransferRunner(resumed, drive, config).run()
    assert second["status"] == "complete"
    assert second["resumed_bytes_this_run"] == config["chunk_size"]
    assert len(drive.sessions) == 1
    assert len(drive.content) == 1
    resumed.close()


def test_unchanged_resume_uses_change_feed_not_full_listing(job):
    store, config, local = job()
    drive = FakeDrive()
    drive.add_file("a.bin", b"data")
    first = TransferRunner(store, drive, config).run()
    calls = drive.page_calls
    second = TransferRunner(store, drive, config).run()
    assert first["status"] == second["status"] == "complete"
    assert drive.page_calls == calls
    assert second["bytes_this_run"] == 0
    assert (local / "a.bin").read_bytes() == b"data"
    store.close()


@pytest.mark.parametrize(
    "name", ["../escape", "CON.txt", "trailing.", "bad:name", "nested\\escape"]
)
def test_unsafe_drive_names_do_not_escape_destination(job, name):
    store, config, _local = job()
    drive = FakeDrive()
    drive.add_file(name, b"unsafe")
    report = TransferRunner(store, drive, config).run()
    assert report["status"] in {"failed", "incomplete"}
    assert drive.download_calls == []
    store.close()


def test_case_collisions_are_detected_before_any_download(job):
    store, config, _local = job()
    drive = FakeDrive()
    drive.add_file("A.txt", b"one")
    drive.add_file("a.txt", b"two")
    report = TransferRunner(store, drive, config).run()
    assert report["status"] == "incomplete"
    assert drive.download_calls == []
    store.close()


def test_case_colliding_directories_do_not_merge(job):
    store, config, _local = job()
    drive = FakeDrive()
    one, two = drive.create_folder("A", "root"), drive.create_folder("a", "root")
    drive.add_file("one.txt", b"1", one)
    drive.add_file("two.txt", b"2", two)
    report = TransferRunner(store, drive, config).run()
    assert report["counts"]["conflict"]["files"] == 2
    assert not drive.download_calls
    # Static namespace conflicts must stay blocked on resume.
    assert TransferRunner(store, drive, config).run()["status"] == "incomplete"
    assert not drive.download_calls
    store.close()


def test_export_conversion_and_receipt_recovery(job):
    store, config, local = job(export_docs="office")
    drive = FakeDrive()
    identity = drive.add_file("Notes", b"example exported document")
    drive.items[identity]["mimeType"] = "application/vnd.google-apps.document"
    drive.items[identity].pop("md5Checksum")
    drive.items[identity].pop("size")
    original = Downloader._publish

    def publish_then_interrupt(self, *args):
        original(self, *args)
        self.control.cancel()
        raise Cancelled()

    with patch.object(Downloader, "_publish", publish_then_interrupt):
        assert TransferRunner(store, drive, config).run()["status"] == "cancelled"
    assert (local / "Notes.docx").read_bytes() == drive.content[identity]
    report = TransferRunner(store, drive, config).run()
    assert report["counts"]["exported"]["files"] == 1
    assert report["bytes_this_run"] == 0
    assert not drive.download_calls
    store.close()


def test_binary_rename_crash_recovers_without_download(job):
    store, config, local = job()
    drive = FakeDrive()
    drive.add_file("file", b"payload")
    original = Downloader._publish

    def publish_then_interrupt(self, *args):
        original(self, *args)
        self.control.cancel()
        raise Cancelled()

    with patch.object(Downloader, "_publish", publish_then_interrupt):
        TransferRunner(store, drive, config).run()
    assert (local / "file").read_bytes() == b"payload"
    report = TransferRunner(store, drive, config).run()
    assert report["counts"]["copied"]["files"] == 1
    assert len(drive.download_calls) == 1
    store.close()


def test_changed_drive_source_not_published(job):
    store, config, local = job()
    drive = FakeDrive()
    identity = drive.add_file("file", b"payload")
    original = drive.download_range

    def mutate(*args):
        data = original(*args)
        drive.items[identity]["version"] = "2"
        return data

    drive.download_range = mutate
    assert TransferRunner(store, drive, config).run()["status"] == "incomplete"
    assert not (local / "file").exists()
    store.close()


def test_no_clobber_when_destination_appears_at_publication(job):
    store, config, local = job()
    drive = FakeDrive()
    drive.add_file("file", b"download")
    original = Downloader._publish

    def race(self, partial, target, relative):
        target.write_bytes(b"other writer")
        original(self, partial, target, relative)

    with patch.object(Downloader, "_publish", race):
        assert TransferRunner(store, drive, config).run()["status"] == "incomplete"
    assert (local / "file").read_bytes() == b"other writer"
    store.close()


def test_missing_skip_is_rechecked_on_resume(job):
    store, config, local = job()
    local.mkdir()
    (local / "file").write_bytes(b"other")
    drive = FakeDrive()
    drive.add_file("file", b"right")
    TransferRunner(store, drive, config).run()
    (local / "file").unlink()
    report = TransferRunner(store, drive, config).run()
    assert report["counts"]["copied"]["files"] == 1
    assert (local / "file").read_bytes() == b"right"
    store.close()


def test_expired_inventory_cursor_restarts_only_interrupted_folder(job):
    store, config, _local = job()
    drive = FakeDrive()
    one = drive.add_file("a", b"a")
    two = drive.add_file("b", b"b")

    def page(identity, token=None):
        if token:
            drive.control.cancel()
            raise Cancelled()
        return {"files": [drive.items[one]], "nextPageToken": "expired"}

    drive.folder_page = page
    assert TransferRunner(store, drive, config).run()["status"] == "cancelled"
    calls = []

    def restarted_page(identity, token=None):
        calls.append(token)
        if token:
            raise DriveApiError(400, "invalid cursor")
        return {"files": [drive.items[one], drive.items[two]]}

    drive.folder_page = restarted_page
    assert TransferRunner(store, drive, config).run()["status"] == "complete"
    assert calls == ["expired", None]
    store.close()


def test_changed_folder_refresh_does_not_rescan_unrelated_subtree(job):
    store, config, _local = job()
    drive = FakeDrive()
    folder = drive.create_folder("nested", "root")
    drive.add_file("one", b"1", folder)
    TransferRunner(store, drive, config).run()
    calls = drive.page_calls
    drive.add_file("two", b"2", folder)
    report = TransferRunner(store, drive, config).run()
    assert drive.page_calls == calls + 1
    assert sum(c["files"] for c in report["counts"].values()) == 1  # Frozen manifest.
    assert store.db.execute("SELECT 1 FROM remote WHERE path='nested/two'").fetchone()
    store.close()


def test_state_and_client_credentials_are_excluded(job):
    store, config, local = job("upload")
    credential = local / "private-client.json"
    credential.write_text("secret")
    config["excluded_files"] = [str(credential)]
    config["state_dir"] = str(local / "state")
    (local / "state").mkdir()
    (local / "state" / "token").write_text("secret")
    (local / "public").write_bytes(b"payload")
    drive = FakeDrive()
    report = TransferRunner(store, drive, config).run()
    assert report["counts"]["copied"]["files"] == 1
    assert list(drive.content.values()) == [b"payload"]
    store.close()


def test_scan_errors_are_in_summary_and_human_report(job):
    import io

    from rich.console import Console

    from gdrivecopy.terminal import render_report

    store, config, local = job("upload")

    def denied_walk(root, *, followlinks, onerror):
        onerror(PermissionError(13, "permission denied", str(local / "unreadable")))
        return iter(())

    with patch("gdrivecopy.scanner.os.walk", denied_walk):
        report = TransferRunner(store, FakeDrive(), config).run()
    assert report["status"] == "incomplete"
    assert report["scan_errors"] == 1
    assert "unreadable" in report["scan_errors_sample"][0]
    output = io.StringIO()
    render_report(report, Console(file=output, width=120))
    assert "permission denied" in output.getvalue()
    store.close()


def test_disk_sync_failure_never_advances_checkpoint_or_publishes(job):
    store, config, local = job()
    drive = FakeDrive()
    drive.add_file("file", b"payload")
    original = os.fsync

    def fail_on_partial(fd):
        raise OSError("disk full")

    # Only fail while transferring; report fsync must remain usable.
    original_range = drive.download_range

    def range_then_fail(*args):
        payload = original_range(*args)
        os.fsync = fail_on_partial
        return payload

    drive.download_range = range_then_fail
    runner = TransferRunner(store, drive, config)
    original_dispatch = runner._dispatch

    def dispatch(*args):
        try:
            original_dispatch(*args)
        finally:
            os.fsync = original

    with patch.object(runner, "_dispatch", dispatch):
        assert runner.run()["status"] == "incomplete"
    assert store.file("file")["offset"] == 0
    assert not (local / "file").exists()
    store.close()


def test_same_size_skip_is_not_reported_as_verified_copy(job):
    store, config, local = job()
    local.mkdir()
    (local / "same.txt").write_bytes(b"local")
    drive = FakeDrive()
    drive.add_file("same.txt", b"other")
    report = TransferRunner(store, drive, config).run()
    assert report["counts"] == {"skipped_size": {"files": 1, "bytes": 5}}
    assert drive.download_calls == []
    # A missing receipt was historically saved as the string 'null'. Reusing
    # the job must preserve the requested fast comparison and avoid disk reads.
    store.update_file("same.txt", proof="null")
    with patch("gdrivecopy.transfer.hashlib.md5", side_effect=AssertionError("unexpected hash")):
        resumed = TransferRunner(store, drive, config).run()
    assert resumed["counts"] == report["counts"]
    assert resumed["status"] == "complete"
    store.close()


@pytest.mark.parametrize("protocol", ["multipart", "resumable"])
@pytest.mark.parametrize("changed_source", [False, True])
def test_wrong_upload_identity_never_trashes_unrelated_file(job, protocol, changed_source):
    from gdrivecopy.drive import UploadResponse

    store, config, local = job("upload")
    source = local / "payload"
    source.write_bytes(b"x" * (8 * 1024 * 1024 + 1) if protocol == "resumable" else b"data")
    drive = FakeDrive()
    unrelated = drive.add_file("unrelated", b"must survive")
    method = "upload_chunk" if protocol == "resumable" else "multipart_upload"
    original = getattr(drive, method)

    def wrong_response(*args, **kwargs):
        response = original(*args, **kwargs)
        if response is not None:
            if changed_source:
                source.write_bytes(b"changed")
            return UploadResponse(unrelated, None)
        return response

    with patch.object(drive, method, wrong_response), patch.object(drive, "trash_file") as trash:
        report = TransferRunner(store, drive, config).run()
    assert report["status"] == "incomplete"
    assert "different upload identity" in store.file("payload")["error"]
    trash.assert_not_called()
    store.close()


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_interrupted_manifest_is_rebuilt_without_obsolete_rows(job, direction):
    store, config, local = job(direction)
    drive = FakeDrive()
    if direction == "upload":
        (local / "current").write_bytes(b"current")
    else:
        drive.add_file("current", b"current")
    # Simulate a crash after a planning batch, before plan_complete. No copy
    # has started, so obsolete entries and collision decisions are disposable.
    store.add_files([("obsolete", {}, 99)])
    with store.transaction() as db:
        db.execute("CREATE TABLE targets(name TEXT PRIMARY KEY,path TEXT NOT NULL)")
        db.execute("INSERT INTO targets VALUES('current','obsolete')")
    report = TransferRunner(store, drive, config).run()
    assert report["status"] == "complete"
    assert report["counts"] == {"copied": {"files": 1, "bytes": 7}}
    assert store.file("obsolete") is None
    store.close()


def test_receipt_rehash_updates_mtime_for_subsequent_fast_resume(job):
    store, config, local = job()
    drive = FakeDrive()
    drive.add_file("file", b"payload")
    TransferRunner(store, drive, config).run()
    target = local / "file"
    stamp = target.stat().st_mtime_ns + 10_000_000_000
    os.utime(target, ns=(stamp, stamp))
    assert TransferRunner(store, drive, config).run()["status"] == "complete"
    with patch("gdrivecopy.transfer.hashlib.md5", side_effect=AssertionError("unexpected hash")):
        assert TransferRunner(store, drive, config).run()["status"] == "complete"
    assert len(drive.download_calls) == 1
    store.close()


@pytest.mark.parametrize("name", ["COM¹.txt", "LPT².txt", "com³", ".GDRIVECOPY-user.parts"])
def test_reserved_windows_and_internal_names_are_blocked_before_download(job, name):
    store, config, _local = job()
    drive = FakeDrive()
    drive.add_file(name, b"payload")
    report = TransferRunner(store, drive, config).run()
    assert report["counts"]["conflict"]["files"] == 1
    assert not drive.download_calls
    store.close()


def test_dry_run_classifies_without_creating_payloads_or_upload_ids(job):
    store, config, local = job("upload", dry_run=True)
    (local / "a.txt").write_text("hello")
    drive = FakeDrive()
    report = TransferRunner(store, drive, config).run()
    assert report["status"] == "planned"
    assert report["counts"]["pending"]["files"] == 1
    assert drive.upload_calls == 0
    assert store.file("a.txt")["remote_id"] is None
    store.close()


def test_os_job_lock_releases_without_deleting_lock_file(tmp_path):
    path = tmp_path / "job.lock"
    with JobLock(path), pytest.raises(RuntimeError), JobLock(path):
        pass
    with JobLock(path):
        assert path.exists()


def test_progress_keeps_only_active_files_and_bounded_errors():
    model = ProgressModel()
    for i in range(1000):
        model.event("start", str(i), size=10)
        model.event("error", str(i), size=10, message="failed")
    snapshot = model.snapshot()
    assert snapshot["active"] == {}
    assert len(snapshot["errors"]) == 5


def test_audit_does_not_include_resumable_capabilities(job):
    store, config, local = job("upload")
    (local / "large.bin").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    drive = FakeDrive()
    drive.cancel_after_chunk = True
    TransferRunner(store, drive, config).run()
    payload = json.dumps(list(store.rows("SELECT * FROM events")))
    assert "upload_id=" not in payload
    assert "session_uri" not in payload
    store.close()


def test_retry_event_redacts_secrets_before_audit_and_dashboard(job):
    store, config, _local = job()
    runner = TransferRunner(store, FakeDrive(), config)
    runner.control.emit(
        "retry",
        "file",
        message="https://www.googleapis.com/upload?upload_id=secret&access_token=private",
    )
    payload = json.dumps(list(store.rows("SELECT * FROM events")))
    assert "secret" not in payload and "private" not in payload
    assert "redacted" in payload
    store.close()


def test_download_rate_limit_retries_and_quota_pauses(job):
    store, config, local = job()
    drive = FakeDrive()
    drive.add_file("file", b"payload")
    original = drive.download_range
    runner = TransferRunner(store, drive, config)
    with (
        patch.object(
            drive, "download_range", side_effect=[RateLimitError(429, "rate"), b"payload"]
        ),
        patch.object(runner.control, "wait"),
    ):
        report = runner.run()
    assert report["status"] == "complete"
    assert report["retries"] == 1
    (local / "file").unlink()
    with patch.object(drive, "download_range", side_effect=QuotaLimitError(403, "quota")):
        report = TransferRunner(store, drive, config).run()
    assert report["status"] == "paused"
    assert report["counts"]["pending"]["files"] == 1
    drive.download_range = original
    assert TransferRunner(store, drive, config).run()["status"] == "complete"
    store.close()


def test_inventory_quota_pauses_before_payload_and_can_resume(job):
    store, config, _local = job()
    drive = FakeDrive()
    drive.add_file("file", b"payload")
    with patch.object(drive, "folder_page", side_effect=QuotaLimitError(403, "daily quota")):
        report = TransferRunner(store, drive, config).run()
    assert report["status"] == "paused"
    assert "quota" in report["stop_reason"]
    assert not drive.download_calls
    assert TransferRunner(store, drive, config).run()["status"] == "complete"
    store.close()


def test_download_refreshes_only_once_after_401(job):
    store, config, _local = job()
    drive = FakeDrive()
    drive.add_file("file", b"payload")
    with (
        patch.object(drive, "download_range", side_effect=DriveApiError(401, "expired")),
        patch.object(drive, "refresh_credentials") as refresh,
    ):
        assert TransferRunner(store, drive, config).run()["status"] == "incomplete"
    refresh.assert_called_once()
    store.close()


def test_exhausted_single_attempt_is_failure_not_successful_cancellation(job):
    store, config, _local = job(retries=1)
    drive = FakeDrive()
    drive.add_file("file", b"payload")
    with patch.object(drive, "download_range", side_effect=DriveApiError(401, "expired")):
        report = TransferRunner(store, drive, config).run()
    assert report["status"] == "incomplete"
    assert report["counts"]["failed"]["files"] == 1
    store.close()


def test_zero_byte_binary_is_verified_without_range_request(job):
    store, config, local = job()
    drive = FakeDrive()
    drive.add_file("empty", b"")
    assert TransferRunner(store, drive, config).run()["status"] == "complete"
    assert (local / "empty").read_bytes() == b""
    assert not drive.download_calls
    store.close()


def test_partial_shorter_than_checkpoint_restarts_from_zero(job):
    store, config, local = job()
    drive = FakeDrive()
    drive.add_file("file", b"x" * 700_000)
    drive.cancel_after_range = True
    TransferRunner(store, drive, config).run()
    partial = next(local.glob(".gdrivecopy-*.parts/*.part"))
    partial.write_bytes(b"short")
    assert TransferRunner(store, drive, config).run()["status"] == "complete"
    assert drive.download_calls[1][1] == 0
    store.close()


def test_native_document_is_explicitly_unsupported_without_export_option(job):
    store, config, _local = job()
    drive = FakeDrive()
    identity = drive.add_file("Document", b"native")
    drive.items[identity]["mimeType"] = "application/vnd.google-apps.document"
    report = TransferRunner(store, drive, config).run()
    assert report["status"] == "incomplete"
    assert report["counts"]["unsupported"]["files"] == 1
    assert not drive.download_calls
    store.close()


def test_disk_backed_manifest_handles_large_byte_counts_in_batches(tmp_path):
    store = JobStore(tmp_path)
    for batch in range(20):
        store.add_files(
            [(f"file-{batch * 500 + i:06}", {"path": str(i)}, 4 * 1024**4) for i in range(500)]
        )
    counts = store.counts()
    assert counts["pending"] == {"files": 10000, "bytes": 10000 * 4 * 1024**4}
    assert sum(1 for _ in store.rows("SELECT path FROM files")) == 10000
    store.close()
