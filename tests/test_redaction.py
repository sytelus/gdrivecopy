"""Secrets must stay out of diagnostics, including failures before job startup."""

import io
import logging

import pytest

from gdrivecopy.cli import main
from gdrivecopy.redaction import RedactedLog, safe_error


@pytest.mark.parametrize(
    "message",
    [
        "HTTP failed ?upload_id=secret&path=keep",
        "refresh_token=secret access_token=secret",
        '{"client_secret": "secret", "message": "keep"}',
        "{'id_token': 'secret', 'reason': 'keep'}",
        "Authorization: Bearer secret",
    ],
)
def test_common_secret_representations_are_redacted(message):
    assert "secret" not in safe_error(message).replace("client_secret", "")
    assert "[redacted]" in safe_error(message)


def test_exception_traceback_is_redacted_in_log():
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(RedactedLog())
    logger = logging.Logger("test")
    logger.addHandler(handler)
    try:
        raise RuntimeError("access_token=secret")
    except RuntimeError:
        logger.exception("Request failed: %s", "upload_id=secret")
    handler.close()
    assert "secret" not in output.getvalue()
    assert "RuntimeError" in output.getvalue()


def test_pre_job_cli_failure_is_redacted(monkeypatch, capsys):
    def fail(_args):
        raise ValueError("access_token=secret")

    monkeypatch.setattr("gdrivecopy.commands.execute", fail)
    with pytest.raises(SystemExit) as exc:
        main(["jobs"])
    assert exc.value.code == 1
    output = capsys.readouterr().err
    assert "secret" not in output and "[redacted]" in output
