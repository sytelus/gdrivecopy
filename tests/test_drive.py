"""Tests for gdrivecopy.drive -- Google Drive API client with mocked HTTP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from gdrivecopy.drive import (
    FOLDER_MIME,
    DriveApiError,
    DriveClient,
    DrivePathConflictError,
    QuotaLimitError,
    RateLimitError,
    UploadResponse,
    UploadSessionError,
    UploadStatus,
    _retry_transient,
)

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
    with (
        patch("gdrivecopy.drive.build") as mock_build,
        patch("gdrivecopy.drive.AuthorizedSession") as mock_session_cls,
    ):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_service.files().get().execute.return_value = {
            "id": "root-id",
            "mimeType": FOLDER_MIME,
            "trashed": False,
        }
        mock_service.files().generateIds().execute.return_value = {"ids": ["new-folder-123"]}
        mock_http = MagicMock()
        mock_session_cls.return_value = mock_http

        creds = MagicMock()
        client = DriveClient(creds)

    return client, mock_service, mock_http


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------


class TestListAll:
    @pytest.mark.parametrize(
        "metadata",
        [
            {"id": "root-id", "mimeType": "text/plain"},
            {"id": "root-id", "mimeType": FOLDER_MIME, "trashed": True},
            {"id": "root-id", "mimeType": FOLDER_MIME, "driveId": "shared-drive"},
        ],
    )
    def test_invalid_destination_fails_before_listing(self, metadata):
        client, svc, _ = _make_client()
        svc.files().get().execute.return_value = metadata
        with pytest.raises(DriveApiError):
            client.list_all("root-id")
        svc.files().list.assert_not_called()

    def test_incomplete_search_does_not_look_like_empty_destination(self):
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {"files": [], "incompleteSearch": True}
        with pytest.raises(DriveApiError, match="incomplete"):
            client.list_all("root-id")

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

    def test_missing_size_is_preserved_as_unknown(self) -> None:
        """A native Drive item must not masquerade as a zero-byte local file."""
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {
            "files": [
                {
                    "id": "native-1",
                    "name": "notes",
                    "mimeType": "application/vnd.google-apps.document",
                }
            ]
        }

        file_map, _ = client.list_all("root-id")

        assert file_map["notes"].size is None

    def test_invalid_size_raises_api_error(self) -> None:
        """Malformed size metadata fails closed instead of corrupting classification."""
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {
            "files": [{"id": "bad-1", "name": "bad", "size": "NaN", "mimeType": "text/plain"}]
        }

        with pytest.raises(DriveApiError, match="invalid size"):
            client.list_all("root-id")

    def test_missing_list_metadata_raises_api_error(self) -> None:
        """Malformed listing items produce a controlled API error, not a KeyError."""
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {
            "files": [{"id": "broken", "name": "missing-mime"}]
        }

        with pytest.raises(DriveApiError, match="MIME type"):
            client.list_all("root-id")

    def test_duplicate_names_raise_path_conflict(self) -> None:
        """Duplicate Drive names are ambiguous and must not be silently overwritten."""
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {
            "files": [
                {"id": "f1", "name": "same.txt", "size": "1", "mimeType": "text/plain"},
                {"id": "f2", "name": "same.txt", "size": "1", "mimeType": "text/plain"},
            ]
        }

        with pytest.raises(DrivePathConflictError, match="Duplicate"):
            client.list_all("root-id")

    def test_slash_in_name_raises_path_conflict(self) -> None:
        """A slash in a Drive name cannot map unambiguously to a local path."""
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {
            "files": [
                {
                    "id": "f1",
                    "name": "folder/name.txt",
                    "size": "1",
                    "mimeType": "text/plain",
                }
            ]
        }

        with pytest.raises(DrivePathConflictError, match="contains '/'"):
            client.list_all("root-id")


# ---------------------------------------------------------------------------
# create_folder
# ---------------------------------------------------------------------------


class TestCreateFolder:
    def test_returns_folder_id(self) -> None:
        """create_folder sends the right metadata and returns the new ID."""
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {"files": []}
        svc.files().create().execute.return_value = {"id": "new-folder-123"}

        result = client.create_folder("my_folder", "parent-id")
        assert result == "new-folder-123"

    def test_reuses_existing_folder(self) -> None:
        """Retrying an ambiguous create can recover the existing folder ID."""
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {
            "files": [{"id": "existing-id", "mimeType": FOLDER_MIME}]
        }

        assert client.create_folder("photos", "parent-id") == "existing-id"
        svc.files().create.assert_not_called()

    def test_duplicate_existing_folders_raise(self) -> None:
        """An ambiguous destination hierarchy fails instead of choosing randomly."""
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {"files": [{"id": "one"}, {"id": "two"}]}

        with pytest.raises(DrivePathConflictError, match="Multiple Drive"):
            client.create_folder("photos", "parent-id")

    def test_partial_empty_page_does_not_create_duplicate(self):
        client, svc, _ = _make_client()
        svc.files().list().execute.side_effect = [
            {"files": [], "nextPageToken": "next"},
            {"files": [{"id": "existing", "mimeType": FOLDER_MIME}]},
        ]
        assert client.create_folder("photos", "parent-id") == "existing"
        svc.files().create.assert_not_called()

    def test_file_with_folder_name_is_a_conflict(self):
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {
            "files": [{"id": "file", "mimeType": "text/plain"}]
        }
        with pytest.raises(DrivePathConflictError):
            client.create_folder("photos", "parent-id")
        svc.files().create.assert_not_called()

    def test_lost_create_response_retries_same_generated_id(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response

        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {"files": []}
        svc.files().create().execute.side_effect = [
            OSError("response lost"),
            HttpError(Response({"status": "409"}), b'{"error":{"message":"exists"}}'),
        ]
        svc.files().create.reset_mock()
        with pytest.raises(DriveApiError):
            client.create_folder("photos", "parent-id")
        assert client.create_folder("photos", "parent-id") == "new-folder-123"
        bodies = [call.kwargs["body"] for call in svc.files().create.call_args_list]
        assert [body["id"] for body in bodies] == ["new-folder-123", "new-folder-123"]

    def test_create_requires_returned_folder_id(self) -> None:
        """A malformed folder-create response fails with a controlled error."""
        client, svc, _ = _make_client()
        svc.files().list().execute.return_value = {"files": []}
        svc.files().create().execute.return_value = {}

        with pytest.raises(DriveApiError, match="folder id"):
            client.create_folder("photos", "parent-id")


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
            headers={
                "Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=session-abc"
            },
        )

        uri = client.initiate_resumable_upload(name="test.bin", parent_id="p1", file_size=1024)
        assert uri == "https://www.googleapis.com/upload/drive/v3/files?upload_id=session-abc"

    def test_preserves_timestamps(self) -> None:
        """Created/modified times are sent in the JSON metadata."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=200,
            json_body={},
            headers={"Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=s1"},
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

    def test_missing_location_header_raises_clear_error(self) -> None:
        """A malformed successful response must not leak a KeyError."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(status_code=200, json_body={})

        with pytest.raises(DriveApiError, match="Location"):
            client.initiate_resumable_upload("f.txt", "p", 10)

    def test_request_has_timeout(self) -> None:
        """Raw upload requests must have a finite network timeout."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=200,
            json_body={},
            headers={
                "Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=session"
            },
        )

        client.initiate_resumable_upload("f.txt", "p", 10)

        assert http.post.call_args.kwargs["timeout"] == (10, 300)


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

        result = client.upload_chunk(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", b"data", 0, 4
        )
        assert isinstance(result, UploadResponse)
        assert result.file_id == "file-xyz"
        assert result.md5_checksum == "abc"

    def test_intermediate_chunk_returns_none(self) -> None:
        """A 308 response means more chunks are needed -- returns None."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=308,
            headers={"Range": "bytes=0-3"},
        )

        result = client.upload_chunk(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", b"data", 0, 100
        )
        assert result is None

    def test_error_raises(self) -> None:
        """An error status (e.g. 403) raises DriveApiError."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=403,
            json_body={"error": {"message": "Forbidden", "errors": [{"reason": "forbidden"}]}},
        )

        with pytest.raises(DriveApiError):
            client.upload_chunk(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", b"data", 0, 4
            )

    @pytest.mark.parametrize("status_code", [400, 404, 410])
    def test_invalid_session_requires_restart(self, status_code: int) -> None:
        """Session-invalid responses are distinguished from ordinary API errors."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=status_code,
            json_body={"error": {"message": "Invalid upload session"}},
        )

        with pytest.raises(UploadSessionError) as exc_info:
            client.upload_chunk(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", b"data", 0, 4
            )
        assert exc_info.value.status == status_code

    def test_partial_acknowledgement_raises(self) -> None:
        """The caller must not skip bytes when Drive acknowledges only part of a chunk."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=308,
            headers={"Range": "bytes=0-1"},
        )

        with pytest.raises(DriveApiError, match="confirmed 2 bytes"):
            client.upload_chunk(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", b"data", 0, 100
            )

    def test_early_completion_raises(self) -> None:
        """A completion response before the declared final byte is inconsistent."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=200,
            json_body={"id": "unexpected"},
        )

        with pytest.raises(DriveApiError, match="completed upload early"):
            client.upload_chunk(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", b"data", 0, 100
            )

    def test_invalid_completion_metadata_raises_drive_error(self) -> None:
        """Malformed success JSON must not leak a KeyError from the client."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(status_code=200, json_body={})

        with pytest.raises(DriveApiError, match="invalid file metadata"):
            client.upload_chunk(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", b"data", 0, 4
            )

    def test_unexpected_success_status_raises_drive_error(self) -> None:
        """An undocumented 2xx response cannot be mistaken for progress."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(status_code=204)

        with pytest.raises(DriveApiError, match="Unexpected resumable upload status"):
            client.upload_chunk(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", b"data", 0, 4
            )


# ---------------------------------------------------------------------------
# query_upload_status
# ---------------------------------------------------------------------------


class TestQueryUploadStatus:
    @pytest.mark.parametrize(
        "uri",
        [
            "https://example.com/upload/drive/v3/files?upload_id=secret",
            "http://www.googleapis.com/upload/drive/v3/files?upload_id=secret",
            "https://www.googleapis.com@evil.test/upload/drive/v3/files",
            "https://www.googleapis.com:444/upload/drive/v3/files",
            "https://www.googleapis.com/drive/v3/files/existing",
        ],
    )
    def test_untrusted_session_url_never_receives_credentials(self, uri):
        client, _, http = _make_client()
        with pytest.raises(UploadSessionError):
            client.query_upload_status(uri, 100)
        http.put.assert_not_called()

    def test_partial_upload(self) -> None:
        """A 308 with Range header returns the confirmed byte count."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=308,
            headers={"Range": "bytes=0-499"},
        )

        status = client.query_upload_status(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", 1000
        )
        assert status == UploadStatus(confirmed_bytes=500)
        assert http.put.call_args.kwargs["headers"] == {"Content-Range": "bytes */1000"}

    def test_nothing_received(self) -> None:
        """A 308 without Range header means zero bytes confirmed."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(status_code=308, headers={})

        status = client.query_upload_status(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", 1000
        )
        assert status == UploadStatus(confirmed_bytes=0)

    def test_already_completed(self) -> None:
        """A 200 response means the upload was already finished."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=200,
            json_body={"id": "done-id"},
        )

        status = client.query_upload_status(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", 500
        )
        assert status.confirmed_bytes == 500
        assert status.completed == UploadResponse("done-id", None)

    @pytest.mark.parametrize("status_code", [400, 404, 410])
    def test_rejected_session_raises(self, status_code: int) -> None:
        """A rejected resumable status query requires a fresh session."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=status_code,
            json_body={"error": {"message": "Rejected session"}},
        )

        with pytest.raises(UploadSessionError) as exc_info:
            client.query_upload_status(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", 1000
            )
        assert exc_info.value.status == status_code

    @pytest.mark.parametrize("range_header", ["garbage", "bytes=10-20", "bytes=0-1000"])
    def test_invalid_range_raises(self, range_header: str) -> None:
        """Malformed or out-of-bounds progress responses fail safely."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(
            status_code=308,
            headers={"Range": range_header},
        )

        with pytest.raises(DriveApiError):
            client.query_upload_status(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", 1000
            )

    def test_completed_response_requires_file_id(self) -> None:
        """Completed sessions need file metadata for safe checksum verification."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(status_code=200, json_body={})

        with pytest.raises(DriveApiError, match="invalid file metadata"):
            client.query_upload_status(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", 1000
            )

    def test_unexpected_status_response_raises_drive_error(self) -> None:
        """An undocumented status-query response produces a controlled error."""
        client, _, http = _make_client()
        http.put.return_value = _make_response(status_code=204)

        with pytest.raises(DriveApiError, match="Unexpected resumable status-query"):
            client.query_upload_status(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", 1000
            )


# ---------------------------------------------------------------------------
# multipart_upload
# ---------------------------------------------------------------------------


class TestMultipartUpload:
    def test_grown_file_is_bounded_and_not_sent(self, tmp_path):
        from gdrivecopy.drive import MULTIPART_THRESHOLD

        client, _, http = _make_client()
        path = tmp_path / "grown.bin"
        with path.open("wb") as stream:
            stream.truncate(MULTIPART_THRESHOLD + 1)
        with pytest.raises(OSError, match="size limit"):
            client.multipart_upload(path, path.name, "parent")
        http.post.assert_not_called()

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

    def test_invalid_success_metadata_raises_drive_error(self, tmp_path: Path) -> None:
        """Multipart success still requires a nonempty Drive file ID."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(status_code=200, json_body={"id": ""})
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")

        with pytest.raises(DriveApiError, match="invalid file metadata"):
            client.multipart_upload(file_path, file_path.name, "parent-id")


# ---------------------------------------------------------------------------
# Error handling -- _check_errors
# ---------------------------------------------------------------------------


class TestCheckErrors:
    def test_429_raises_rate_limit_error(self) -> None:
        """HTTP 429 is classified as RateLimitError."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=429,
            json_body={
                "error": {
                    "message": "Rate Limit Exceeded",
                    "errors": [{"reason": "rateLimitExceeded"}],
                }
            },
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

    def test_403_daily_limit_raises_quota_limit_error(self) -> None:
        """HTTP 403 with dailyLimitExceeded is a blocking quota error."""
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

        with pytest.raises(QuotaLimitError):
            client.initiate_resumable_upload("f.txt", "p", 10)

    def test_daily_limit_reason_need_not_be_first(self) -> None:
        """Drive can return multiple reasons; all of them must be inspected."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=403,
            json_body={
                "error": {
                    "message": "quota reached",
                    "errors": [
                        {"reason": "forbidden"},
                        {"reason": "dailyLimitExceeded"},
                    ],
                }
            },
        )

        with pytest.raises(QuotaLimitError):
            client.initiate_resumable_upload("f.txt", "p", 10)

    @pytest.mark.parametrize("reason", ["storageQuotaExceeded", "activeItemCreationLimitExceeded"])
    def test_blocking_account_quotas_raise_quota_error(self, reason: str) -> None:
        """Storage and account item limits stop the run rather than failing every file."""
        client, _, http = _make_client()
        http.post.return_value = _make_response(
            status_code=403,
            json_body={
                "error": {
                    "message": "quota reached",
                    "errors": [{"reason": reason}],
                }
            },
        )

        with pytest.raises(QuotaLimitError):
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
            client.query_upload_status(
                "https://www.googleapis.com/upload/drive/v3/files?upload_id=s", 100
            )
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
            headers={"Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=ok"},
        )

        # Should not raise
        client.initiate_resumable_upload("f.txt", "p", 10)


class TestRetryTransient:
    @patch("gdrivecopy.drive.time.sleep")
    def test_discovery_dns_failures_are_retried(self, _sleep):
        from httplib2 import ServerNotFoundError

        operation = MagicMock(side_effect=[ServerNotFoundError("offline"), "ok"])
        assert _retry_transient(operation) == "ok"
        assert operation.call_count == 2

    @patch("gdrivecopy.drive.time.sleep")
    def test_retries_requests_timeouts(self, mock_sleep: MagicMock) -> None:
        """Discovery operations retry the complete requests transport error family."""
        operation = MagicMock(side_effect=[requests.Timeout("slow"), "ok"])

        assert _retry_transient(operation, "test") == "ok"
        assert operation.call_count == 2
        mock_sleep.assert_called_once()

    @patch("gdrivecopy.drive.time.sleep")
    def test_stops_after_five_attempts(self, mock_sleep: MagicMock) -> None:
        """The retry helper does not make an undocumented extra attempt."""
        operation = MagicMock(side_effect=requests.ConnectionError("offline"))

        with pytest.raises(requests.ConnectionError):
            _retry_transient(operation, "test")

        assert operation.call_count == 5
        assert mock_sleep.call_count == 4
