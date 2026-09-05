"""Installed and frozen apps must work without network fallback during doctor."""

import json
from unittest.mock import patch

from gdrivecopy.cli import main
from gdrivecopy.diagnostics import diagnose


def test_diagnostics_are_offline_and_check_real_local_dependencies():
    with patch("socket.socket.connect", side_effect=AssertionError("Unexpected network access")):
        result = diagnose()
    assert result["ok"]
    assert set(result["checks"]) == {
        "drive_discovery",
        "tls_certificates",
        "sqlite_and_local_state",
    }


def test_diagnostics_report_missing_discovery_without_authentication(capsys):
    import pytest

    with (
        patch("gdrivecopy.diagnostics.build", side_effect=FileNotFoundError("missing discovery")),
        pytest.raises(SystemExit) as exc,
    ):
        main(["doctor", "--json"])
    assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["checks"]["drive_discovery"]["ok"] is False
    assert "missing discovery" in result["checks"]["drive_discovery"]["error"]
