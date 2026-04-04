"""Shared fixtures for gdrivecopy tests.

Provides temporary directories with files, mock credentials, and a mock
DriveClient so that tests never touch the real Google Drive API.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest

from gdrivecopy.drive import DriveClient, UploadResponse
from gdrivecopy.models import (
    DriveFile,
    LocalFile,
    SessionEntry,
    UploadConfig,
    UploadStats,
)


# ---------------------------------------------------------------------------
# Temporary directory with sample files
# ---------------------------------------------------------------------------


@pytest.fixture()
def source_tree(tmp_path: Path) -> Path:
    """Create a temp directory tree with a few regular files.

    Layout::

        tmp_path/
            file_a.txt          (11 bytes)
            subdir/
                file_b.bin      (5 bytes)
                nested/
                    file_c.dat  (3 bytes)

    Returns the root directory path.
    """
    root = tmp_path / "source"
    root.mkdir()

    (root / "file_a.txt").write_text("hello world")

    sub = root / "subdir"
    sub.mkdir()
    (sub / "file_b.bin").write_bytes(b"\x00" * 5)

    nested = sub / "nested"
    nested.mkdir()
    (nested / "file_c.dat").write_bytes(b"\x01\x02\x03")

    return root


@pytest.fixture()
def source_tree_with_symlink(source_tree: Path) -> Path:
    """Add a symlink to the source tree so scanner tests can verify it is skipped."""
    link = source_tree / "link_to_a.txt"
    link.symlink_to(source_tree / "file_a.txt")
    return source_tree


# ---------------------------------------------------------------------------
# Mock Google credentials
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_credentials() -> MagicMock:
    """Return a mock ``google.auth.credentials.Credentials`` object."""
    creds = MagicMock()
    creds.valid = True
    creds.token = "fake-access-token"
    return creds


# ---------------------------------------------------------------------------
# Mock DriveClient
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_drive() -> MagicMock:
    """Return a ``MagicMock`` that quacks like ``DriveClient``.

    All methods return safe defaults.  Tests should override individual
    return values / side effects as needed.
    """
    drive = create_autospec(DriveClient, instance=True)

    # list_all returns empty maps by default
    drive.list_all.return_value = ({}, {"": "root-folder-id"})

    # create_folder returns a predictable id
    drive.create_folder.return_value = "new-folder-id"

    # initiate_resumable_upload returns a fake session URI
    drive.initiate_resumable_upload.return_value = (
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=fake123"
    )

    # upload_chunk returns a completed response by default
    drive.upload_chunk.return_value = UploadResponse(
        file_id="uploaded-file-id",
        md5_checksum="d41d8cd98f00b204e9800998ecf8427e",
    )

    # multipart_upload returns a completed response
    drive.multipart_upload.return_value = UploadResponse(
        file_id="uploaded-file-id",
        md5_checksum=None,
    )

    # query_upload_status returns 0 (nothing confirmed) by default
    drive.query_upload_status.return_value = 0

    # trash_file succeeds silently
    drive.trash_file.return_value = None

    return drive


# ---------------------------------------------------------------------------
# Standard UploadConfig
# ---------------------------------------------------------------------------


@pytest.fixture()
def upload_config(tmp_path: Path, source_tree: Path) -> UploadConfig:
    """Return an ``UploadConfig`` pointing at the temp source tree."""
    return UploadConfig(
        source_dir=source_tree,
        drive_folder_id="root-folder-id",
        transfers=1,
        chunk_size=256 * 1024,  # 256 KiB -- small for tests
        dry_run=False,
        credentials_path=tmp_path / "credentials.json",
        token_path=tmp_path / "token.json",
        session_path=tmp_path / "sessions.json",
        verify_checksum=True,
        quiet=True,
        log_dir=tmp_path,
        log_level="DEBUG",
    )


# ---------------------------------------------------------------------------
# Local file helper
# ---------------------------------------------------------------------------


@pytest.fixture()
def make_local_file(tmp_path: Path):
    """Factory fixture that creates a ``LocalFile`` backed by a real temp file."""

    def _factory(
        name: str = "test.bin",
        content: bytes = b"test-content",
        relative_path: str | None = None,
    ) -> LocalFile:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        stat = p.stat()
        from gdrivecopy.scanner import _iso_from_timestamp

        return LocalFile(
            path=p,
            relative_path=relative_path or name,
            size=len(content),
            mtime=_iso_from_timestamp(stat.st_mtime),
            ctime=_iso_from_timestamp(stat.st_ctime),
        )

    return _factory


# ---------------------------------------------------------------------------
# Session cache file helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_file(tmp_path: Path) -> Path:
    """Return a path to a sessions.json (does not exist yet)."""
    return tmp_path / "sessions.json"


@pytest.fixture()
def populated_session_file(session_file: Path) -> Path:
    """Create a sessions.json with one entry and return the path."""
    data = {
        "photos/img001.jpg": {
            "session_uri": "https://upload.example.com/session1",
            "file_size": 1024,
            "mtime": "2025-01-01T00:00:00+00:00",
        }
    }
    session_file.write_text(json.dumps(data), encoding="utf-8")
    return session_file
