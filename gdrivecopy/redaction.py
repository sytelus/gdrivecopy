"""Shared best-effort secret redaction for CLI errors, logs, and reports."""

from __future__ import annotations

import logging
import re

_KEYS = r"upload_id|access_token|refresh_token|id_token|client_secret"
_FORM_SECRET = re.compile(rf"(?i)((?:{_KEYS})=)[^&\s\"']+")
_QUOTED_SECRET = re.compile(
    rf"(?i)([\"'](?:{_KEYS})[\"']\s*:\s*)(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
)
_BEARER = re.compile(r"(?i)(bearer\s+)[^\s\"']+")


def safe_error(value: object) -> str:
    """Redact common URL/form, JSON/dict, and Authorization representations."""
    message = _FORM_SECRET.sub(r"\1[redacted]", str(value))
    message = _QUOTED_SECRET.sub(r'\1"[redacted]"', message)
    return _BEARER.sub(r"\1[redacted]", message)


class RedactedLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = safe_error(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = safe_error(logging.Formatter().formatException(record.exc_info))
        if record.stack_info:
            record.stack_info = safe_error(record.stack_info)
        return True


def protect_logs(handlers: list[logging.Handler]) -> None:
    for handler in handlers:
        handler.addFilter(RedactedLog())
    # Suppress third-party OAuth debug records before their arbitrary payloads
    # reach a best-effort filter. Never treat redaction as permission to share logs.
    for name in ("google.auth", "google_auth_oauthlib", "oauthlib", "requests_oauthlib", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
