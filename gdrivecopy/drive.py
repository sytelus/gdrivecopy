"""Google Drive API client.

Wraps both the discovery-based ``googleapiclient`` service (for metadata
operations like listing and folder creation) and a raw ``AuthorizedSession``
(for the resumable-upload HTTP protocol).

Transient errors are surfaced to the caller so the ``Uploader`` can apply
its own retry / circuit-breaker logic.
"""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from google.auth.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError

from gdrivecopy.models import DriveFile

logger = logging.getLogger(__name__)

T = TypeVar("T")

DRIVE_API_VERSION = "v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
_MAX_API_RETRIES = 5


# ------------------------------------------------------------------
# Response / error types
# ------------------------------------------------------------------

@dataclass
class UploadResponse:
    """Result of a completed upload (final chunk returned 200/201)."""

    file_id: str
    md5_checksum: str | None


class DriveApiError(Exception):
    """An HTTP error from the Drive API with status code attached."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class DailyLimitError(DriveApiError):
    """Raised when the 750 GB daily upload cap is hit."""


class RateLimitError(DriveApiError):
    """Raised on 429 / 403-rateLimitExceeded so the caller can back off."""


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _file_metadata(
    name: str,
    parent_id: str,
    created_time: str | None = None,
    modified_time: str | None = None,
) -> dict[str, Any]:
    """Build the metadata dict shared by uploads and copies."""
    meta: dict[str, Any] = {"name": name, "parents": [parent_id]}
    if created_time:
        meta["createdTime"] = created_time
    if modified_time:
        meta["modifiedTime"] = modified_time
    return meta


def _retry_transient(func: Callable[[], T], description: str = "") -> T:
    """Call *func* with retries on transient Google API errors.

    Handles both ``HttpError`` (from the discovery client) and
    ``requests`` connection errors.  Non-retryable errors propagate
    immediately.
    """
    for attempt in range(_MAX_API_RETRIES):
        try:
            return func()
        except HttpError as exc:
            status = exc.resp.status
            if status in (429, 500, 502, 503, 504) or (
                status == 403 and _is_rate_limit_reason(exc)
            ):
                delay = min(30, 2 ** attempt) * random.random()
                logger.warning(
                    "Transient error during %s (attempt %d/%d), retrying in %.1fs: %s",
                    description, attempt + 1, _MAX_API_RETRIES, delay, exc,
                )
                time.sleep(delay)
                continue
            raise
        except (ConnectionError, OSError) as exc:
            delay = min(30, 2 ** attempt) * random.random()
            logger.warning(
                "Connection error during %s (attempt %d/%d), retrying in %.1fs: %s",
                description, attempt + 1, _MAX_API_RETRIES, delay, exc,
            )
            time.sleep(delay)
    # Last attempt — let exceptions propagate.
    return func()


def _is_rate_limit_reason(exc: HttpError) -> bool:
    """Check if an HttpError is a rate-limit error (not daily limit)."""
    try:
        body = json.loads(exc.content)
        reason = body.get("error", {}).get("errors", [{}])[0].get("reason", "")
        return reason in ("rateLimitExceeded", "userRateLimitExceeded")
    except Exception:
        return False


# ------------------------------------------------------------------
# Client
# ------------------------------------------------------------------

class DriveClient:
    """Thin wrapper around the Google Drive API v3.

    Args:
        credentials: Authenticated OAuth 2.0 credentials.
    """

    def __init__(self, credentials: Credentials) -> None:
        self._creds = credentials
        self._service: Resource = build(
            "drive", DRIVE_API_VERSION, credentials=credentials
        )
        # Limit library-internal refresh retries to enforce single-retry-layer.
        self._http = AuthorizedSession(credentials, max_refresh_attempts=1)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_all(
        self, root_folder_id: str
    ) -> tuple[dict[str, DriveFile], dict[str, str]]:
        """Recursively list every file and folder under *root_folder_id*.

        Returns:
            ``(file_map, folder_map)`` where *file_map* maps
            ``relative_path -> DriveFile`` and *folder_map* maps
            ``relative_path/ -> drive_folder_id``.
        """
        file_map: dict[str, DriveFile] = {}
        folder_map: dict[str, str] = {"": root_folder_id}
        self._list_recursive(root_folder_id, "", file_map, folder_map)
        logger.info(
            "Drive scan complete: %d files, %d folders",
            len(file_map),
            len(folder_map) - 1,
        )
        return file_map, folder_map

    def _list_recursive(
        self,
        folder_id: str,
        prefix: str,
        file_map: dict[str, DriveFile],
        folder_map: dict[str, str],
    ) -> None:
        page_token: str | None = None
        while True:
            resp = _retry_transient(
                lambda pt=page_token: (
                    self._service.files()
                    .list(
                        q=f"'{folder_id}' in parents and trashed=false",
                        fields=(
                            "nextPageToken, "
                            "files(id, name, size, md5Checksum, mimeType)"
                        ),
                        pageSize=1000,
                        pageToken=pt,
                    )
                    .execute()
                ),
                description=f"listing folder {folder_id}",
            )
            for item in resp.get("files", []):
                name: str = item["name"]
                rel = f"{prefix}{name}"

                if item["mimeType"] == FOLDER_MIME:
                    folder_rel = f"{rel}/"
                    folder_map[folder_rel] = item["id"]
                    self._list_recursive(item["id"], folder_rel, file_map, folder_map)
                else:
                    file_map[rel] = DriveFile(
                        id=item["id"],
                        name=name,
                        size=int(item.get("size", 0)),
                        md5_checksum=item.get("md5Checksum"),
                    )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    # ------------------------------------------------------------------
    # Folder creation
    # ------------------------------------------------------------------

    def create_folder(self, name: str, parent_id: str) -> str:
        """Create a folder on Drive and return its ID."""
        body: dict[str, Any] = {
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id],
        }
        result = _retry_transient(
            lambda: self._service.files().create(body=body, fields="id").execute(),
            description=f"creating folder {name}",
        )
        folder_id: str = result["id"]
        logger.debug("Created folder %s (id=%s) under %s", name, folder_id, parent_id)
        return folder_id

    # ------------------------------------------------------------------
    # Resumable upload
    # ------------------------------------------------------------------

    def initiate_resumable_upload(
        self,
        name: str,
        parent_id: str,
        file_size: int,
        mime_type: str | None = None,
        created_time: str | None = None,
        modified_time: str | None = None,
    ) -> str:
        """Start a resumable upload session and return the session URI."""
        metadata = _file_metadata(name, parent_id, created_time, modified_time)
        resp = self._http.post(
            f"{UPLOAD_URL}?uploadType=resumable&fields=id,md5Checksum",
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type or _guess_mime(name),
                "X-Upload-Content-Length": str(file_size),
            },
            data=json.dumps(metadata),
        )
        self._check_errors(resp)
        session_uri = resp.headers["Location"]
        logger.debug("Initiated resumable upload: %s", name)
        return session_uri

    def upload_chunk(
        self,
        session_uri: str,
        data: bytes,
        start: int,
        total: int,
    ) -> UploadResponse | None:
        """Upload a single chunk.

        Returns an ``UploadResponse`` on the final chunk (200/201),
        ``None`` on 308 (more chunks expected).
        """
        end = start + len(data) - 1
        resp = self._http.put(
            session_uri,
            headers={
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            },
            data=data,
        )
        if resp.status_code in (200, 201):
            body = resp.json()
            return UploadResponse(file_id=body["id"], md5_checksum=body.get("md5Checksum"))
        if resp.status_code == 308:
            return None
        self._check_errors(resp)
        return None  # unreachable

    def query_upload_status(self, session_uri: str, total: int) -> int:
        """Query a resumable session for the confirmed byte count.

        Returns the number of bytes confirmed, or ``total`` if the upload
        already completed.

        Raises:
            DriveApiError: On session expiry (404) or other error.
        """
        resp = self._http.put(
            session_uri,
            headers={"Content-Range": f"*/{total}"},
        )
        if resp.status_code == 308:
            range_header = resp.headers.get("Range", "")
            if range_header.startswith("bytes=0-"):
                return int(range_header.split("-")[1]) + 1
            return 0
        if resp.status_code in (200, 201):
            return total
        self._check_errors(resp)
        return 0  # unreachable

    # ------------------------------------------------------------------
    # Multipart upload (small files)
    # ------------------------------------------------------------------

    def multipart_upload(
        self,
        file_path: Path,
        name: str,
        parent_id: str,
        mime_type: str | None = None,
        created_time: str | None = None,
        modified_time: str | None = None,
    ) -> UploadResponse:
        """Upload a small file (≤ 8 MiB) in a single multipart request."""
        metadata = _file_metadata(name, parent_id, created_time, modified_time)
        content_type = mime_type or _guess_mime(name)

        boundary = f"gdrivecopy_{uuid.uuid4().hex}"
        body = io.BytesIO()
        body.write(f"--{boundary}\r\n".encode())
        body.write(b"Content-Type: application/json; charset=UTF-8\r\n\r\n")
        body.write(json.dumps(metadata).encode())
        body.write(b"\r\n")
        body.write(f"--{boundary}\r\n".encode())
        body.write(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.write(file_path.read_bytes())
        body.write(b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())

        resp = self._http.post(
            f"{UPLOAD_URL}?uploadType=multipart&fields=id,md5Checksum",
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            data=body.getvalue(),
        )
        self._check_errors(resp)
        result = resp.json()
        return UploadResponse(file_id=result["id"], md5_checksum=result.get("md5Checksum"))

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def trash_file(self, file_id: str) -> None:
        """Move a file to the Drive trash (recoverable)."""
        _retry_transient(
            lambda: self._service.files().update(
                fileId=file_id, body={"trashed": True}
            ).execute(),
            description=f"trashing file {file_id}",
        )
        logger.info("Trashed file %s", file_id)

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def _check_errors(self, resp: Any) -> None:
        """Raise a typed exception for error HTTP responses."""
        if resp.status_code < 400:
            return

        try:
            body = resp.json()
            message = body.get("error", {}).get("message", resp.text)
            errors = body.get("error", {}).get("errors", [])
            reason = errors[0].get("reason", "") if errors else ""
        except Exception:
            message = resp.text
            reason = ""

        status = resp.status_code
        full_msg = f"HTTP {status}: {message}"

        if status == 403 and "dailyLimitExceeded" in reason:
            raise DailyLimitError(status, full_msg)
        if status == 429 or (
            status == 403
            and reason in ("rateLimitExceeded", "userRateLimitExceeded")
        ):
            raise RateLimitError(status, full_msg)
        raise DriveApiError(status, full_msg)
