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
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

from google.auth.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession, Request
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error
from requests.exceptions import RequestException
from urllib3.exceptions import HTTPError as Urllib3Error

from gdrivecopy.control import RunControl
from gdrivecopy.models import DriveFile
from gdrivecopy.session import validate_session_uri

logger = logging.getLogger(__name__)

T = TypeVar("T")

DRIVE_API_VERSION = "v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
MULTIPART_THRESHOLD = 8 * 1024 * 1024  # 8 MiB; also bounds each in-memory payload.
_MAX_API_ATTEMPTS = 5
_HTTP_TIMEOUT = (10, 300)  # connection timeout, response timeout (seconds)
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
_BLOCKING_QUOTA_REASONS = frozenset(
    {
        "activeItemCreationLimitExceeded",
        "dailyLimitExceeded",
        "storageQuotaExceeded",
    }
)
_RANGE_PATTERN = re.compile(r"^bytes=0-(\d+)$")


# ------------------------------------------------------------------
# Response / error types
# ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UploadResponse:
    """Result of a completed upload (final chunk returned 200/201)."""

    file_id: str
    md5_checksum: str | None


@dataclass(frozen=True, slots=True)
class UploadStatus:
    """Server-confirmed progress for a resumable upload session."""

    confirmed_bytes: int
    completed: UploadResponse | None = None


class DriveApiError(Exception):
    """An HTTP error from the Drive API with status code attached."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class QuotaLimitError(DriveApiError):
    """Raised for a nonretryable Drive storage, item, or daily API quota."""


class RateLimitError(DriveApiError):
    """Raised on 429 / 403-rateLimitExceeded so the caller can back off."""


class UploadSessionError(DriveApiError):
    """Raised when a resumable upload session must be restarted."""


class DrivePathConflictError(DriveApiError):
    """Raised when Drive contains paths that cannot map safely to a local tree."""


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
    """Build the metadata shared by both upload protocols."""
    meta: dict[str, Any] = {"name": name, "parents": [parent_id]}
    if created_time:
        meta["createdTime"] = created_time
    if modified_time:
        meta["modifiedTime"] = modified_time
    return meta


def _escape_query_literal(value: str) -> str:
    """Escape a string embedded in a Drive API single-quoted query literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _retry_transient(
    func: Callable[[], T], description: str = "", control: RunControl | None = None
) -> T:
    """Call *func* with retries on transient Google API errors.

    Handles both ``HttpError`` (from the discovery client) and
    ``requests`` connection errors.  Non-retryable errors propagate
    immediately.
    """
    for attempt in range(1, _MAX_API_ATTEMPTS + 1):
        if control:
            control.check()
        try:
            return func()
        except HttpError as exc:
            status = exc.resp.status
            reasons = _error_reasons(exc.content)
            api_error = _make_api_error(status, str(exc), reasons)
            if isinstance(api_error, QuotaLimitError):
                raise api_error from exc
            retryable = status in _TRANSIENT_HTTP_STATUSES or (
                status == 403 and bool(reasons & _RATE_LIMIT_REASONS)
            )
            if not retryable or attempt == _MAX_API_ATTEMPTS:
                raise api_error from exc
            error: Exception = api_error
        except (RequestException, HttpLib2Error, ConnectionError, OSError) as exc:
            if attempt == _MAX_API_ATTEMPTS:
                raise
            error = exc

        delay = min(30, 2 ** (attempt - 1)) * random.random()
        logger.warning(
            "Transient error during %s (attempt %d/%d), retrying in %.1fs: %s",
            description,
            attempt,
            _MAX_API_ATTEMPTS,
            delay,
            error,
        )
        if control:
            control.emit("retry", message=f"Retrying {description}", attempt=attempt, delay=delay)
            control.wait(delay)
        else:
            time.sleep(delay)

    raise AssertionError("retry loop exited unexpectedly")


def _error_reasons(content: bytes | str) -> set[str]:
    """Extract all Drive error reasons from an error response body."""
    try:
        body = json.loads(content)
        errors = body.get("error", {}).get("errors", [])
        return {
            item.get("reason", "")
            for item in errors
            if isinstance(item, dict) and item.get("reason")
        }
    except (AttributeError, json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return set()


def _make_api_error(status: int, message: str, reasons: set[str]) -> DriveApiError:
    """Classify one Drive HTTP failure consistently across both transports."""
    if status == 403 and reasons & _BLOCKING_QUOTA_REASONS:
        return QuotaLimitError(status, message)
    if status == 429 or (status == 403 and reasons & _RATE_LIMIT_REASONS):
        return RateLimitError(status, message)
    return DriveApiError(status, message)


def _confirmed_bytes(range_header: str, total: int) -> int:
    """Parse a resumable-upload ``Range`` header into a byte count."""
    if not range_header:
        return 0
    match = _RANGE_PATTERN.fullmatch(range_header.strip())
    if match is None:
        raise DriveApiError(502, f"Invalid resumable upload Range header: {range_header!r}")
    confirmed = int(match.group(1)) + 1
    if confirmed > total:
        raise DriveApiError(
            502,
            f"Resumable upload confirmed {confirmed} bytes for a {total}-byte file",
        )
    return confirmed


def _parse_upload_response(resp: Any) -> UploadResponse:
    """Validate the file metadata returned when an upload completes."""
    try:
        body = resp.json()
        file_id = body["id"]
        md5_checksum = body.get("md5Checksum")
        if not isinstance(file_id, str) or not file_id:
            raise ValueError("missing file id")
        if md5_checksum is not None and not isinstance(md5_checksum, str):
            raise ValueError("invalid MD5 checksum")
        return UploadResponse(file_id=file_id, md5_checksum=md5_checksum)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise DriveApiError(502, "Completed upload returned invalid file metadata") from exc


def _required_string(value: Any, field: str) -> str:
    """Validate a required nonempty string in a Drive API response."""
    if not isinstance(value, str) or not value:
        raise DriveApiError(502, f"Drive returned an invalid or missing {field}")
    return value


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
        self._service: Resource = build("drive", DRIVE_API_VERSION, credentials=credentials)
        self._service_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._http_local = threading.local()
        self._folder_creation_lock = threading.Lock()
        self._folder_ids: dict[tuple[str, str], str] = {}
        self.control: RunControl | None = None
        # ``requests.Session`` instances are not guaranteed to be thread-safe,
        # so each upload worker gets its own authorized session.
        self._http_local.session = self._new_http_session()

    def _retry(self, operation, description=""):
        return _retry_transient(operation, description, self.control)

    def _new_http_session(self) -> AuthorizedSession:
        """Create a minimally retrying HTTP session for the current thread."""
        # The uploader owns the single 401 retry budget. Disable hidden retries
        # here, especially for one-request multipart creations.
        return AuthorizedSession(self._creds, max_refresh_attempts=0)

    def _http_session(self) -> AuthorizedSession:
        """Return the authorized HTTP session owned by the current thread."""
        session = getattr(self._http_local, "session", None)
        if session is None:
            session = self._new_http_session()
            self._http_local.session = session
        return session

    def _execute_service_request(self, request_factory: Callable[[], T]) -> T:
        """Run one discovery-client request under its thread-safety lock."""
        with self._service_lock:
            if self.control is not None:
                self.control.check()
            return request_factory()

    def account_info(self) -> dict:
        """Return server-confirmed account identity, never infer it from a filename."""
        response = self._retry(
            lambda: self._execute_service_request(
                lambda: (
                    self._service.about()
                    .get(fields="user(displayName,emailAddress,permissionId),storageQuota")
                    .execute()
                )
            ),
            "identifying account",
        )
        user = response.get("user", {})
        _required_string(user.get("emailAddress"), "account email")
        _required_string(user.get("permissionId"), "account identity")
        return response

    def file_metadata(self, file_id: str) -> dict:
        return self._retry(
            lambda: self._execute_service_request(
                lambda: (
                    self._service.files()
                    .get(
                        fileId=file_id,
                        fields=(
                            "id,name,mimeType,size,md5Checksum,modifiedTime,createdTime,version,parents,"
                            "trashed,driveId,capabilities(canDownload)"
                        ),
                    )
                    .execute()
                )
            ),
            "reading file metadata",
        )

    def folder_page(self, folder_id: str, token: str | None = None) -> dict:
        """One checkpointable listing page, shared by initial and incremental scans."""
        response = self._retry(
            lambda: self._execute_service_request(
                lambda: (
                    self._service.files()
                    .list(
                        q=f"'{_escape_query_literal(folder_id)}' in parents and trashed=false",
                        pageSize=1000,
                        pageToken=token,
                        fields=(
                            "nextPageToken,incompleteSearch,files(id,name,mimeType,size,md5Checksum,"
                            "modifiedTime,createdTime,version,parents,capabilities(canDownload))"
                        ),
                    )
                    .execute()
                )
            ),
            "scanning folder",
        )
        if not isinstance(response, dict) or not isinstance(response.get("files", []), list):
            raise DriveApiError(502, "Invalid Drive listing page")
        if response.get("incompleteSearch"):
            raise DriveApiError(502, "Drive returned an incomplete search")
        return response

    def change_token(self) -> str:
        result = self._retry(
            lambda: self._execute_service_request(
                lambda: self._service.changes().getStartPageToken().execute()
            ),
            "starting Drive change tracking",
        )
        return _required_string(result.get("startPageToken"), "change token")

    def change_page(self, token: str) -> dict:
        return self._retry(
            lambda: self._execute_service_request(
                lambda: (
                    self._service.changes()
                    .list(
                        pageToken=token,
                        pageSize=1000,
                        includeRemoved=True,
                        fields="nextPageToken,newStartPageToken,changes(fileId,removed,file(id,parents,trashed,mimeType))",
                    )
                    .execute()
                )
            ),
            "refreshing Drive changes",
        )

    def generate_ids(self, count: int = 100) -> list[str]:
        result = self._retry(
            lambda: self._execute_service_request(
                lambda: self._service.files().generateIds(count=count, space="drive").execute()
            ),
            "reserving upload identities",
        )
        ids = result.get("ids")
        if (
            not isinstance(ids, list)
            or len(ids) != count
            or any(not isinstance(value, str) for value in ids)
            or len(set(ids)) != count
        ):
            raise DriveApiError(502, "Drive returned invalid generated IDs")
        return [_required_string(value, "generated ID") for value in ids]

    def upload_result(self, file_id: str) -> UploadResponse:
        metadata = self.file_metadata(file_id)
        if metadata.get("trashed"):
            raise DriveApiError(409, "Reserved upload is in trash; recovery requires review")
        return UploadResponse(
            _required_string(metadata.get("id"), "file ID"), metadata.get("md5Checksum")
        )

    def download_range(self, file_id: str, start: int, length: int, total: int) -> bytes:
        """Fetch and validate one bounded binary range without transparent decoding."""
        if not 0 <= start < total or length <= 0:
            raise ValueError("Invalid download range")
        end = min(total, start + length) - 1
        if self.control:
            self.control.check()
        with self._http_session().get(
            f"https://www.googleapis.com/drive/v3/files/{quote(file_id, safe='')}?alt=media",
            headers={"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"},
            timeout=_HTTP_TIMEOUT,
            allow_redirects=False,
            stream=True,
        ) as response:
            self._check_errors(response)
            expected = end - start + 1
            if response.status_code == 206:
                if response.headers.get("Content-Range") != f"bytes {start}-{end}/{total}":
                    raise DriveApiError(502, "Invalid download Content-Range")
            elif response.status_code != 200 or start != 0 or expected != total:
                raise DriveApiError(502, "Drive ignored the requested download range")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise DriveApiError(502, "Unexpected compressed download response")
            try:
                payload = response.raw.read(expected + 1, decode_content=False)
            except Urllib3Error as exc:
                raise RequestException("Download connection interrupted") from exc
            if len(payload) != expected:
                raise DriveApiError(502, "Download response length did not match its range")
            return payload

    def export_document(self, file_id: str, mime_type: str) -> bytes:
        """Export Google-native content; exports have no byte-range resume or Drive MD5."""
        with self._http_session().get(
            f"https://www.googleapis.com/drive/v3/files/{quote(file_id, safe='')}/export",
            params={"mimeType": mime_type},
            headers={"Accept-Encoding": "identity"},
            timeout=_HTTP_TIMEOUT,
            allow_redirects=False,
            stream=True,
        ) as response:
            self._check_errors(response)
            if response.status_code != 200:
                raise DriveApiError(502, "Unexpected export response")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise DriveApiError(502, "Unexpected compressed export response")
            try:
                content = response.raw.read(10 * 1024 * 1024 + 1, decode_content=False)
            except Urllib3Error as exc:
                raise RequestException("Export connection interrupted") from exc
            if len(content) > 10 * 1024 * 1024:
                raise DriveApiError(
                    400, "Google document export exceeds the supported 10 MiB limit"
                )
            length = response.headers.get("Content-Length")
            if length is not None and (not str(length).isdigit() or int(length) != len(content)):
                raise DriveApiError(502, "Export response length did not match Content-Length")
            return content

    def refresh_credentials(self) -> None:
        """Refresh OAuth credentials, serializing concurrent 401 recovery."""
        with self._refresh_lock:
            self._creds.refresh(Request())

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_all(self, root_folder_id: str) -> tuple[dict[str, DriveFile], dict[str, str]]:
        """Recursively list every file and folder under *root_folder_id*.

        Returns:
            ``(file_map, folder_map)`` where *file_map* maps
            ``relative_path -> DriveFile`` and *folder_map* maps
            ``relative_path/ -> drive_folder_id``.
        """
        root = self._retry(
            lambda: self._execute_service_request(
                lambda: (
                    self._service.files()
                    .get(fileId=root_folder_id, fields="id,mimeType,trashed,driveId")
                    .execute()
                )
            ),
            description="validating destination folder",
        )
        if not isinstance(root, dict) or root.get("mimeType") != FOLDER_MIME or root.get("trashed"):
            raise DrivePathConflictError(409, "Destination must be an existing, untrashed folder")
        if root.get("driveId"):
            raise DrivePathConflictError(409, "Shared-drive destinations are not supported")
        # Resolve aliases such as 'root' so a cached session is tied to this
        # account's real parent ID even if the OAuth account changes later.
        root_folder_id = _required_string(root.get("id"), "destination folder id")
        file_map: dict[str, DriveFile] = {}
        folder_map: dict[str, str] = {"": root_folder_id}
        self._walk_tree(root_folder_id, file_map, folder_map)
        logger.info(
            "Drive scan complete: %d files, %d folders",
            len(file_map),
            len(folder_map) - 1,
        )
        return file_map, folder_map

    def _list_children(self, query: str) -> Iterator[dict[str, Any]]:
        """Read complete, validated pages, including empty intermediate pages."""
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            resp = self._retry(
                lambda pt=page_token: self._execute_service_request(
                    lambda: (
                        self._service.files()
                        .list(
                            q=query,
                            fields="nextPageToken,incompleteSearch,files(id,name,size,md5Checksum,mimeType)",
                            pageSize=1000,
                            pageToken=pt,
                        )
                        .execute()
                    )
                ),
                description="listing Drive children",
            )
            if not isinstance(resp, dict) or not isinstance(resp.get("files", []), list):
                raise DriveApiError(502, "Drive returned an invalid listing page")
            if resp.get("incompleteSearch"):
                raise DriveApiError(502, "Drive returned an incomplete search; refusing to upload")
            for item in resp.get("files", []):
                if not isinstance(item, dict):
                    raise DriveApiError(502, "Drive returned invalid file metadata while listing")
                yield item
            page_token = resp.get("nextPageToken")
            if not page_token:
                return
            if not isinstance(page_token, str) or page_token in seen_tokens:
                raise DriveApiError(502, "Drive returned an invalid or repeated page token")
            seen_tokens.add(page_token)

    def _walk_tree(
        self,
        folder_id: str,
        file_map: dict[str, DriveFile],
        folder_map: dict[str, str],
    ) -> None:
        """Walk with an explicit stack so depth does not consume Python frames."""
        pending = [(folder_id, "")]
        seen_folders = {folder_id}
        while pending:
            folder_id, prefix = pending.pop()
            query = f"'{_escape_query_literal(folder_id)}' in parents and trashed=false"
            for item in self._list_children(query):
                name = _required_string(item.get("name"), "file name")
                item_id = _required_string(item.get("id"), "file id")
                mime_type = _required_string(item.get("mimeType"), "MIME type")
                rel = f"{prefix}{name}"

                if "/" in name:
                    raise DrivePathConflictError(
                        409,
                        f"Drive item name contains '/'; cannot map safely: {rel!r}",
                    )
                if name in (".", ".."):
                    raise DrivePathConflictError(409, f"Ambiguous Drive item name: {rel!r}")
                if rel in file_map or f"{rel}/" in folder_map:
                    raise DrivePathConflictError(409, f"Duplicate or ambiguous Drive path: {rel!r}")

                if mime_type == FOLDER_MIME:
                    folder_rel = f"{rel}/"
                    folder_map[folder_rel] = item_id
                    if item_id in seen_folders:
                        raise DrivePathConflictError(409, "Drive folder appeared more than once")
                    seen_folders.add(item_id)
                    pending.append((item_id, folder_rel))
                else:
                    raw_size = item.get("size")
                    try:
                        if raw_size is not None and (
                            isinstance(raw_size, bool)
                            or not isinstance(raw_size, (str, int))
                            or not re.fullmatch(r"[0-9]+", str(raw_size))
                        ):
                            raise ValueError
                        size = int(raw_size) if raw_size is not None else None
                    except (TypeError, ValueError) as exc:
                        raise DriveApiError(
                            502,
                            f"Drive returned an invalid size for {rel!r}: {raw_size!r}",
                        ) from exc
                    file_map[rel] = DriveFile(
                        id=item_id,
                        name=name,
                        # Google-native documents and shortcuts can omit size.
                        # Preserve that as unknown instead of confusing the
                        # item with an empty local file.
                        size=size,
                        md5_checksum=item.get("md5Checksum"),
                    )

    # ------------------------------------------------------------------
    # Folder creation
    # ------------------------------------------------------------------

    def create_folder(self, name: str, parent_id: str) -> str:
        """Reuse a unique child, or retry creation with a stable generated ID."""
        with self._folder_creation_lock:
            return self._create_folder(name, parent_id)

    def _create_folder(self, name: str, parent_id: str) -> str:
        escaped_name = _escape_query_literal(name)
        escaped_parent = _escape_query_literal(parent_id)
        query = f"'{escaped_parent}' in parents and name='{escaped_name}' and trashed=false"
        existing = list(self._list_children(query))

        if len(existing) > 1:
            raise DrivePathConflictError(
                409,
                f"Multiple Drive items named {name!r} exist under parent {parent_id}",
            )
        if existing:
            first = existing[0]
            if first.get("mimeType") != FOLDER_MIME:
                raise DrivePathConflictError(409, f"Drive file blocks folder {name!r}")
            folder_id = _required_string(first.get("id"), "folder id")
            logger.info("Reusing folder %s (id=%s) under %s", name, folder_id, parent_id)
            return folder_id

        key = (parent_id, name)
        if key not in self._folder_ids:
            result = self._retry(
                lambda: self._execute_service_request(
                    lambda: self._service.files().generateIds(count=1, space="drive").execute()
                ),
                description="generating folder id",
            )
            ids = result.get("ids") if isinstance(result, dict) else None
            if not isinstance(ids, list) or len(ids) != 1:
                raise DriveApiError(502, "Drive returned invalid generated folder ids")
            self._folder_ids[key] = _required_string(ids[0], "generated folder id")
        folder_id = self._folder_ids[key]
        body: dict[str, Any] = {
            "id": folder_id,
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id],
        }
        try:
            # Name lookup can lag after a lost response. The generated ID
            # makes retries idempotent even before the folder appears in lists.
            result = self._execute_service_request(
                lambda: self._service.files().create(body=body, fields="id").execute()
            )
        except HttpError as exc:
            if exc.resp.status == 409:
                return folder_id  # This client reserved the ID before creation.
            raise _make_api_error(exc.resp.status, str(exc), _error_reasons(exc.content)) from exc
        except (RequestException, HttpLib2Error, ConnectionError, OSError) as exc:
            raise DriveApiError(503, f"Connection error while creating folder {name!r}") from exc
        returned_id = _required_string(
            result.get("id") if isinstance(result, dict) else None,
            "folder id",
        )
        if returned_id != folder_id:
            raise DriveApiError(502, "Drive returned a different folder id than requested")
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
        file_id: str | None = None,
    ) -> str:
        """Start a resumable upload session and return the session URI."""
        metadata = _file_metadata(name, parent_id, created_time, modified_time)
        if file_id is not None:
            metadata["id"] = file_id
        resp = self._http_session().post(
            f"{UPLOAD_URL}?uploadType=resumable&fields=id,md5Checksum",
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type or _guess_mime(name),
                "X-Upload-Content-Length": str(file_size),
            },
            data=json.dumps(metadata),
            timeout=_HTTP_TIMEOUT,
            allow_redirects=False,
        )
        self._check_errors(resp)
        if resp.status_code != 200:
            raise DriveApiError(
                502, f"Unexpected upload initiation status: HTTP {resp.status_code}"
            )
        session_uri = resp.headers.get("Location")
        if not session_uri:
            raise DriveApiError(502, "Resumable upload response omitted Location header")
        try:
            validate_session_uri(session_uri)
        except ValueError as exc:
            raise DriveApiError(502, "Drive returned an invalid upload Location") from exc
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
        self._validate_session_uri(session_uri)
        end = start + len(data) - 1
        resp = self._http_session().put(
            session_uri,
            headers={
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            },
            data=data,
            timeout=_HTTP_TIMEOUT,
            allow_redirects=False,
        )
        if resp.status_code in (200, 201):
            if end + 1 != total:
                raise DriveApiError(
                    502,
                    f"Drive completed upload early at byte {end} of {total}",
                )
            return _parse_upload_response(resp)
        if resp.status_code == 308:
            confirmed = _confirmed_bytes(resp.headers.get("Range", ""), total)
            expected = end + 1
            if confirmed != expected:
                raise DriveApiError(
                    502,
                    f"Drive confirmed {confirmed} bytes after sending through byte {end}",
                )
            return None
        if resp.status_code in (400, 404, 410):
            try:
                self._check_errors(resp)
            except DriveApiError as exc:
                raise UploadSessionError(exc.status, str(exc)) from exc
        if resp.status_code < 400:
            raise DriveApiError(
                502,
                f"Unexpected resumable upload status: HTTP {resp.status_code}",
            )
        self._check_errors(resp)
        raise AssertionError("error response did not raise")

    @staticmethod
    def _validate_session_uri(uri: str) -> None:
        try:
            validate_session_uri(uri)
        except ValueError as exc:
            raise UploadSessionError(400, "Invalid or untrusted Drive upload session URI") from exc

    def query_upload_status(self, session_uri: str, total: int) -> UploadStatus:
        """Query a resumable session for the confirmed byte count.

        Returns confirmed progress and, for a completed upload, the uploaded
        file metadata needed for checksum verification.

        Raises:
            UploadSessionError: When the session is expired or invalid.
            DriveApiError: On other errors.
        """
        self._validate_session_uri(session_uri)
        resp = self._http_session().put(
            session_uri,
            headers={"Content-Range": f"bytes */{total}"},
            timeout=_HTTP_TIMEOUT,
            allow_redirects=False,
        )
        if resp.status_code == 308:
            return UploadStatus(
                confirmed_bytes=_confirmed_bytes(resp.headers.get("Range", ""), total)
            )
        if resp.status_code in (200, 201):
            return UploadStatus(
                confirmed_bytes=total,
                completed=_parse_upload_response(resp),
            )
        if resp.status_code in (400, 404, 410):
            try:
                self._check_errors(resp)
            except DriveApiError as exc:
                raise UploadSessionError(exc.status, str(exc)) from exc
        if resp.status_code < 400:
            raise DriveApiError(
                502,
                f"Unexpected resumable status-query response: HTTP {resp.status_code}",
            )
        self._check_errors(resp)
        raise AssertionError("error response did not raise")

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
        file_id: str | None = None,
    ) -> UploadResponse:
        """Upload a small file (≤ 8 MiB) in a single multipart request."""
        metadata = _file_metadata(name, parent_id, created_time, modified_time)
        if file_id is not None:
            metadata["id"] = file_id
        content_type = mime_type or _guess_mime(name)

        boundary = f"gdrivecopy_{uuid.uuid4().hex}"
        body = io.BytesIO()
        body.write(f"--{boundary}\r\n".encode())
        body.write(b"Content-Type: application/json; charset=UTF-8\r\n\r\n")
        body.write(json.dumps(metadata).encode())
        body.write(b"\r\n")
        body.write(f"--{boundary}\r\n".encode())
        body.write(f"Content-Type: {content_type}\r\n\r\n".encode())
        with file_path.open("rb") as stream:
            content = stream.read(MULTIPART_THRESHOLD + 1)
        if len(content) > MULTIPART_THRESHOLD:
            raise OSError("Source exceeded the multipart size limit after scanning")
        body.write(content)
        body.write(b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())

        resp = self._http_session().post(
            f"{UPLOAD_URL}?uploadType=multipart&fields=id,md5Checksum",
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            data=body.getvalue(),
            timeout=_HTTP_TIMEOUT,
            allow_redirects=False,
        )
        if resp.status_code == 409 and file_id is not None:
            return self.upload_result(file_id)
        self._check_errors(resp)
        if resp.status_code not in (200, 201):
            raise DriveApiError(502, f"Unexpected multipart upload status: HTTP {resp.status_code}")
        return _parse_upload_response(resp)

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def trash_file(self, file_id: str) -> None:
        """Move a file to the Drive trash (recoverable)."""
        self._retry(
            lambda: self._execute_service_request(
                lambda: (
                    self._service.files().update(fileId=file_id, body={"trashed": True}).execute()
                )
            ),
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
            reasons = _error_reasons(resp.text)
        except (AttributeError, TypeError, ValueError):
            message = resp.text
            reasons = set()

        status = resp.status_code
        full_msg = f"HTTP {status}: {message}"

        raise _make_api_error(status, full_msg, reasons)
