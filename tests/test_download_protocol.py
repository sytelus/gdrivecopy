"""Validate HTTP responses independently from the offline transfer server."""

import pytest
from requests.exceptions import RequestException
from urllib3.exceptions import ProtocolError

from gdrivecopy.drive import DriveApiError
from tests.test_drive import _make_client, _make_response


def response_for(http, status, headers, payload):
    response = _make_response(status, headers=headers)
    response.__enter__.return_value = response
    response.raw.read.return_value = payload
    http.get.return_value = response
    return response


def test_exact_range_and_safe_transport():
    client, _service, http = _make_client()
    response = response_for(http, 206, {"Content-Range": "bytes 3-5/9"}, b"abc")
    assert client.download_range("file", 3, 3, 9) == b"abc"
    assert http.get.call_args.kwargs["allow_redirects"] is False
    assert http.get.call_args.kwargs["headers"]["Accept-Encoding"] == "identity"
    response.raw.read.assert_called_once_with(4, decode_content=False)
    response.__exit__.assert_called_once()


@pytest.mark.parametrize(
    ("status", "headers", "payload"),
    [
        (200, {}, b"abc"),
        (206, {"Content-Range": "bytes 0-2/9"}, b"abc"),
        (206, {"Content-Range": "bytes 3-5/9"}, b"ab"),
        (206, {"Content-Range": "bytes 3-5/9"}, b"abcd"),
        (206, {"Content-Range": "bytes 3-5/9", "Content-Encoding": "gzip"}, b"abc"),
        (302, {"Location": "https://example.test"}, b"abc"),
    ],
)
def test_wrong_or_unbounded_range_is_rejected(status, headers, payload):
    client, _service, http = _make_client()
    response_for(http, status, headers, payload)
    with pytest.raises(DriveApiError):
        client.download_range("file", 3, 3, 9)


def test_complete_file_can_use_200_response():
    client, _service, http = _make_client()
    response_for(http, 200, {}, b"abc")
    assert client.download_range("file", 0, 3, 3) == b"abc"


def test_broken_stream_becomes_retryable_transport_error():
    client, _service, http = _make_client()
    response = response_for(http, 206, {"Content-Range": "bytes 0-2/3"}, b"")
    response.raw.read.side_effect = ProtocolError("connection closed")
    with pytest.raises(RequestException):
        client.download_range("file", 0, 3, 3)


def test_native_export_is_bounded():
    client, _service, http = _make_client()
    response = response_for(http, 200, {}, b"converted")
    assert client.export_document("document", "application/pdf") == b"converted"
    response.raw.read.assert_called_once_with(10 * 1024 * 1024 + 1, decode_content=False)
    response.raw.read.return_value = b"x" * (10 * 1024 * 1024 + 1)
    with pytest.raises(DriveApiError, match="limit"):
        client.export_document("document", "application/pdf")


def test_wrong_account_metadata_fails_closed():
    client, service, _http = _make_client()
    service.about().get().execute.return_value = {"user": {"displayName": "No identity"}}
    with pytest.raises(DriveApiError):
        client.account_info()


def test_reserved_id_batch_requires_exact_count():
    client, service, _http = _make_client()
    service.files().generateIds().execute.return_value = {"ids": ["one"]}
    with pytest.raises(DriveApiError):
        client.generate_ids(100)
