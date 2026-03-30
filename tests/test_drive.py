"""Tests for gdrivecopy.drive -- Google Drive API client with mocked HTTP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from gdrivecopy.drive import (
    FOLDER_MIME,
    UPLOAD_URL,
    DailyLimitError,
    DriveApiError,
    DriveClient,
    RateLimitError,
    UploadResponse,
)
from gdrivecopy.models import DriveFile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    status_code: int = 200,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    text: str = "",
) -> MagicMock:
    """Create a mock HTTP response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or json.dumps(json_body or {})
    resp.headers = headers or {}
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


def _make_client() -> tuple[DriveClient, MagicMock, MagicMock]:
    """Create a DriveClient with mocked internals.

    Returns (client, mock_service, mock_http).
    """
    with patch("gdrivecopy.drive.build") as mock_build, \
         patch("gdrivecopy.drive.AuthorizedSession") as mock_session_cls:
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_http = MagicMock()
        mock_session_cls.return_value = mock_http

        creds = MagicMock()
        client = DriveClient(creds)

    return client, mock_service, mock_http


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------


class TestListAll:
    def test_empty_folder(self) -> None:
        """list_all on an empty Drive folder returns empty maps."""
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {"files": []}

        file_map, folder_map = client.list_all("root-id")
        assert file_map == {}
        assert folder_map == {"": "root-id"}

    def test_files_and_folders(self) -> None:
        """list_all maps files by relative path and folders by path/."""
        client, svc, _ = _make_client()

        # First call: root level has one file and one folder
        root_page = {
            "files": [
                {
                    "id": "file1-id",
                    "name": "readme.txt",
                    "size": "42",
                    "md5Checksum": "abc123",
                    "mimeType": "text/plain",
                },
                {
                    "id": "folder1-id",
                    "name": "photos",
                    "mimeType": FOLDER_MIME,
                },
            ]
        }
        # Second call: inside "photos/" folder
        photos_page = {
            "files": [
                {
                    "id": "file2-id",
                    "name": "img.jpg",
                    "size": "1000",
                    "md5Checksum": "def456",
                    "mimeType": "image/jpeg",
                },
            ]
        }

        svc.files().list().execute.side_effect = [root_page, photos_page]

        file_map, folder_map = client.list_all("root-id")

        assert "readme.txt" in file_map
        assert file_map["readme.txt"].size == 42
        assert file_map["readme.txt"].md5_checksum == "abc123"

        assert "photos/img.jpg" in file_map
        assert file_map["photos/img.jpg"].size == 1000

        assert "photos/" in folder_map
        assert folder_map["photos/"] == "folder1-id"

    def test_pagination(self) -> None:
        """list_all follows nextPageToken to retrieve all pages."""
        client, svc, _ = _make_client()

        page1 = {
            "files": [
                {"id": "f1", "name": "a.txt", "size": "1", "mimeType": "text/plain"},
            ],
            "nextPageToken": "page2token",
        }
        page2 = {
            "files": [
                {"id": "f2", "name": "b.txt", "size": "2", "mimeType": "text/plain"},
            ],
        }

        svc.files().list().execute.side_effect = [page1, page2]

        file_map, _ = client.list_all("root-id")
        assert len(file_map) == 2
        assert "a.txt" in file_map
        assert "b.txt" in file_map


# ---------------------------------------------------------------------------
# create_folder
# ---------------------------------------------------------------------------


class TestCreateFolder:
    def test_returns_folder_id(self) -> None:
        """create_folder sends the right metadata and returns the new ID."""
        client, svc, _ = _make_client()
        svc.files().create().execute.return_value = {"id": "new-folder-123"}

        result = client.create_folder("my_folder", "parent-id")
        assert result == "new-folder-123"


# ---------------------------------------------------------------------------
# initiate_resumable_upload
# ---------------------------------------------------------------------------


class TestInitiateResumableUpload:
    def test_returns_session_uri(self) -> None:
        """initiate_resumable_upload returns the Location header."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=200,
            json_body={},
            headers={"Location": "https://upload.example.com/session-abc"},
        )

        uri = client.initiate_resumable_upload(
            name="test.bin", parent_id="p1", file_size=1024
        )
        assert uri == "https://upload.example.com/session-abc"

    def test_preserves_timestamps(self) -> None:
        """Created/modified times are sent in the JSON metadata."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=200,
            json_body={},
            headers={"Location": "https://upload.example.com/s1"},
        )

        client.initiate_resumable_upload(
            name="f.txt",
            parent_id="p",
            file_size=10,
            created_time="2025-01-01T00:00:00Z",
            modified_time="2025-06-01T00:00:00Z",
        )

        call_kwargs = http.post.call_args
        body = json.loads(call_kwargs.kwargs.get("data", call_kwargs[1].get("data", "")))
        assert body["createdTime"] == "2025-01-01T00:00:00Z"
        assert body["modifiedTime"] == "2025-06-01T00:00:00Z"

    def test_error_raises(self) -> None:
        """An HTTP error from the initiation request raises DriveApiError."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=500,
            json_body={"error": {"message": "Internal error"}},
        )

        with pytest.raises(DriveApiError) as exc_info:
            client.initiate_resumable_upload("f.txt", "p", 10)
        assert exc_info.value.status == 500


# ---------------------------------------------------------------------------
# upload_chunk
# ---------------------------------------------------------------------------


class TestUploadChunk:
    def test_final_chunk_returns_upload_response(self) -> None:
        """When the server returns 200 the chunk is the last and we get an UploadResponse."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=200,
            json_body={"id": "file-xyz", "md5Checksum": "abc"},
        )

        result = client.upload_chunk("https://upload.example.com/s", b"data", 0, 4)
        assert isinstance(result, UploadResponse)
        assert result.file_id == "file-xyz"
        assert result.md5_checksum == "abc"

    def test_intermediate_chunk_returns_none(self) -> None:
        """A 308 response means more chunks are needed -- returns None."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(status_code=308)

        result = client.upload_chunk("https://upload.example.com/s", b"data", 0, 100)
        assert result is None

    def test_error_raises(self) -> None:
        """An error status (e.g. 403) raises DriveApiError."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=403,
            json_body={"error": {"message": "Forbidden", "errors": [{"reason": "forbidden"}]}},
        )

        with pytest.raises(DriveApiError):
            client.upload_chunk("https://upload.example.com/s", b"data", 0, 4)


# ---------------------------------------------------------------------------
# query_upload_status
# ---------------------------------------------------------------------------


class TestQueryUploadStatus:
    def test_partial_upload(self) -> None:
        """A 308 with Range header returns the confirmed byte count."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=308,
            headers={"Range": "bytes=0-499"},
        )

        confirmed = client.query_upload_status("https://upload.example.com/s", 1000)
        assert confirmed == 500

    def test_nothing_received(self) -> None:
        """A 308 without Range header means zero bytes confirmed."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(status_code=308, headers={})

        confirmed = client.query_upload_status("https://upload.example.com/s", 1000)
        assert confirmed == 0

    def test_already_completed(self) -> None:
        """A 200 response means the upload was already finished."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=200,
            json_body={"id": "done-id"},
        )

        confirmed = client.query_upload_status("https://upload.example.com/s", 500)
        assert confirmed == 500  # returns total

    def test_expired_session_raises(self) -> None:
        """A 404 means the session expired -- should raise DriveApiError."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=404,
            json_body={"error": {"message": "Not Found"}},
        )

        with pytest.raises(DriveApiError) as exc_info:
            client.query_upload_status("https://upload.example.com/s", 1000)
        assert exc_info.value.status == 404


# ---------------------------------------------------------------------------
# multipart_upload
# ---------------------------------------------------------------------------


class TestMultipartUpload:
    def test_successful_upload(self, tmp_path: Path) -> None:
        """multipart_upload reads the file and returns an UploadResponse."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=200,
            json_body={"id": "mp-id", "md5Checksum": "md5abc"},
        )

        f = tmp_path / "small.txt"
        f.write_text("hello")

        result = client.multipart_upload(f, "small.txt", "parent-id")
        assert isinstance(result, UploadResponse)
        assert result.file_id == "mp-id"
        assert result.md5_checksum == "md5abc"

    def test_request_contains_file_content(self, tmp_path: Path) -> None:
        """The multipart body includes the file's bytes."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=200,
            json_body={"id": "id1"},
        )

        f = tmp_path / "content.bin"
        content = b"binary-data-here"
        f.write_bytes(content)

        client.multipart_upload(f, "content.bin", "parent-id")

        call_kwargs = http.post.call_args
        body = call_kwargs.kwargs.get("data", call_kwargs[1].get("data", b""))
        assert content in body

    def test_error_raises(self, tmp_path: Path) -> None:
        """An HTTP error raises DriveApiError."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=500,
            json_body={"error": {"message": "fail"}},
        )

        f = tmp_path / "fail.txt"
        f.write_text("x")

        with pytest.raises(DriveApiError):
            client.multipart_upload(f, "fail.txt", "parent-id")


# ---------------------------------------------------------------------------
# Error handling -- _check_errors
# ---------------------------------------------------------------------------


class TestCheckErrors:
    def test_429_raises_rate_limit_error(self) -> None:
        """HTTP 429 is classified as RateLimitError."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=429,
            json_body={"error": {"message": "Rate Limit Exceeded", "errors": [{"reason": "rateLimitExceeded"}]}},
        )

        with pytest.raises(RateLimitError):
            client.initiate_resumable_upload("f.txt", "p", 10)

    def test_403_rate_limit_raises_rate_limit_error(self) -> None:
        """HTTP 403 with rateLimitExceeded reason is RateLimitError."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=403,
            json_body={
                "error": {
                    "message": "Rate Limit Exceeded",
                    "errors": [{"reason": "rateLimitExceeded"}],
                }
            },
        )

        with pytest.raises(RateLimitError):
            client.initiate_resumable_upload("f.txt", "p", 10)

    def test_403_daily_limit_raises_daily_limit_error(self) -> None:
        """HTTP 403 with dailyLimitExceeded is DailyLimitError."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=403,
            json_body={
                "error": {
                    "message": "dailyLimitExceeded",
                    "errors": [{"reason": "dailyLimitExceeded"}],
                }
            },
        )

        with pytest.raises(DailyLimitError):
            client.initiate_resumable_upload("f.txt", "p", 10)

    def test_500_raises_drive_api_error(self) -> None:
        """HTTP 500 is a generic DriveApiError."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=500,
            json_body={"error": {"message": "Internal Server Error"}},
        )

        with pytest.raises(DriveApiError) as exc_info:
            client.initiate_resumable_upload("f.txt", "p", 10)
        assert exc_info.value.status == 500

    def test_404_raises_drive_api_error(self) -> None:
        """HTTP 404 is a generic DriveApiError with status=404."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=404,
            json_body={"error": {"message": "Not Found"}},
        )

        with pytest.raises(DriveApiError) as exc_info:
            client.query_upload_status("https://upload.example.com/s", 100)
        assert exc_info.value.status == 404

    def test_non_json_error_body(self) -> None:
        """If the error body is not JSON, message falls back to resp.text."""
        client, _, http = _make_client()
        resp = MagicMock()
        resp.status_code = 502
        resp.text = "Bad Gateway (HTML)"
        resp.json.side_effect = ValueError("not json")
        resp.headers = {}
        http.post.return_value = resp

        with pytest.raises(DriveApiError) as exc_info:
            client.initiate_resumable_upload("f.txt", "p", 10)
        assert "502" in str(exc_info.value)

    def test_2xx_does_not_raise(self) -> None:
        """Successful status codes (< 400) do not raise."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=200,
            json_body={},
            headers={"Location": "https://upload.example.com/ok"},
        )

        # Should not raise
        client.initiate_resumable_upload("f.txt", "p", 10)
