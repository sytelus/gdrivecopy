"""Tests for gdrivecopy.auth OAuth cache behavior."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gdrivecopy.auth import SCOPES, authenticate


class TestAuthenticate:
    @patch("gdrivecopy.auth.Credentials.from_authorized_user_file")
    def test_returns_valid_cached_credentials(self, from_file: MagicMock, tmp_path: Path) -> None:
        token_path = tmp_path / "token.json"
        token_path.write_text("{}", encoding="utf-8")
        credentials = MagicMock(valid=True)
        from_file.return_value = credentials

        result = authenticate(tmp_path / "missing.json", token_path)

        assert result is credentials
        from_file.assert_called_once_with(str(token_path), SCOPES)

    def test_missing_credentials_has_clear_error(self, tmp_path: Path) -> None:
        credentials_path = tmp_path / "missing.json"

        with pytest.raises(FileNotFoundError, match="OAuth credentials not found"):
            authenticate(credentials_path, tmp_path / "token.json")

    @patch("gdrivecopy.auth.InstalledAppFlow.from_client_secrets_file")
    def test_consent_flow_saves_token_atomically_in_new_directory(
        self, flow_factory: MagicMock, tmp_path: Path
    ) -> None:
        credentials_path = tmp_path / "credentials.json"
        credentials_path.write_text("{}", encoding="utf-8")
        token_path = tmp_path / "private" / "token.json"
        credentials = MagicMock()
        credentials.to_json.return_value = '{"token": "safe"}'
        flow_factory.return_value.run_local_server.return_value = credentials

        result = authenticate(credentials_path, token_path)

        assert result is credentials
        assert token_path.read_text(encoding="utf-8") == '{"token": "safe"}'
        assert not token_path.with_name("token.json.tmp").exists()
        flow_factory.assert_called_once_with(str(credentials_path), SCOPES)

    @patch("gdrivecopy.auth.InstalledAppFlow.from_client_secrets_file")
    @patch("gdrivecopy.auth.Credentials.from_authorized_user_file")
    def test_corrupt_token_falls_back_to_consent(
        self,
        from_file: MagicMock,
        flow_factory: MagicMock,
        tmp_path: Path,
    ) -> None:
        credentials_path = tmp_path / "credentials.json"
        credentials_path.write_text("{}", encoding="utf-8")
        token_path = tmp_path / "token.json"
        token_path.write_text("broken", encoding="utf-8")
        from_file.side_effect = ValueError("invalid token")
        credentials = MagicMock()
        credentials.to_json.return_value = "{}"
        flow_factory.return_value.run_local_server.return_value = credentials

        assert authenticate(credentials_path, token_path) is credentials

    @patch("gdrivecopy.auth.Request")
    @patch("gdrivecopy.auth.Credentials.from_authorized_user_file")
    def test_expired_token_is_refreshed_and_saved(
        self, from_file: MagicMock, request_cls: MagicMock, tmp_path: Path
    ) -> None:
        token_path = tmp_path / "token.json"
        token_path.write_text("{}", encoding="utf-8")
        credentials = MagicMock(valid=False, expired=True, refresh_token="refresh")
        credentials.refresh.side_effect = lambda _request: setattr(credentials, "valid", True)
        credentials.to_json.return_value = '{"token": "new"}'
        from_file.return_value = credentials

        result = authenticate(tmp_path / "unused.json", token_path)

        assert result is credentials
        credentials.refresh.assert_called_once_with(request_cls.return_value)
        assert token_path.read_text(encoding="utf-8") == '{"token": "new"}'

    @patch("gdrivecopy.auth.InstalledAppFlow.from_client_secrets_file")
    @patch("gdrivecopy.auth.Credentials.from_authorized_user_file")
    def test_revoked_token_requests_fresh_consent(self, from_file, flow_factory, tmp_path):
        from google.auth.exceptions import RefreshError

        token = tmp_path / "token.json"
        client = tmp_path / "credentials.json"
        token.write_text("{}")
        client.write_text("{}")
        old = MagicMock(valid=False, expired=True, refresh_token="expired")
        old.refresh.side_effect = RefreshError("revoked", {"error": "invalid_grant"})
        from_file.return_value = old
        fresh = MagicMock()
        fresh.to_json.return_value = '{"token":"fresh"}'
        flow_factory.return_value.run_local_server.return_value = fresh
        assert authenticate(client, token) is fresh
        assert "fresh" in token.read_text()

    @pytest.mark.parametrize("payload", ["null", "[]", "42"])
    def test_malformed_token_shape_is_ignored(self, tmp_path, payload):
        token = tmp_path / "token.json"
        token.write_text(payload)
        with pytest.raises(FileNotFoundError, match="OAuth credentials not found"):
            authenticate(tmp_path / "missing.json", token)

    @pytest.mark.parametrize("payload", ['{"installed": {}}', "[]", "{broken"])
    def test_malformed_client_json_has_actionable_error(self, tmp_path, payload):
        client = tmp_path / "client.json"
        client.write_text(payload)
        with pytest.raises(ValueError, match="Desktop app client"):
            authenticate(client, tmp_path / "token.json")
