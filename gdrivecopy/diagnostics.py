"""Offline installation checks, including data required by frozen binaries."""

from __future__ import annotations

import json
import platform
import ssl
import tempfile
from pathlib import Path

import certifi
from google.auth.credentials import AnonymousCredentials
from googleapiclient.discovery import build
from rich.console import Console
from rich.table import Table
from rich.text import Text

from gdrivecopy import __version__
from gdrivecopy.jobstore import JobLock, JobStore
from gdrivecopy.persistence import write_text_atomic
from gdrivecopy.transfer import safe_error


def diagnose() -> dict:
    """Exercise local dependencies without authenticating or sending requests."""
    checks = {}

    def check(name, operation):
        try:
            operation()
            checks[name] = {"ok": True}
        except Exception as exc:
            checks[name] = {"ok": False, "error": safe_error(exc)}

    def discovery():
        # Force bundled discovery: a missing data file must fail, not fall back
        # to the network and hide an incomplete release package.
        service = build(
            "drive",
            "v3",
            credentials=AnonymousCredentials(),
            static_discovery=True,
            cache_discovery=False,
        )
        try:
            service.files().get(fileId="offline-check", fields="id")
        finally:
            service.close()

    def persistence():
        with tempfile.TemporaryDirectory(prefix="gdrivecopy-check-") as temporary:
            directory = Path(temporary)
            with JobLock(directory / "check.lock"):
                store = JobStore(directory)
                try:
                    store.set("check", "ok")
                    if store.get("check") != "ok":
                        raise RuntimeError("SQLite round trip failed")
                    if store.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("SQLite integrity check failed")
                finally:
                    store.close()
                path = directory / "atomic.txt"
                write_text_atomic(path, "ok")
                if path.read_text() != "ok":
                    raise RuntimeError("Atomic file round trip failed")

    check("drive_discovery", discovery)
    check("tls_certificates", lambda: ssl.create_default_context(cafile=certifi.where()))
    check("sqlite_and_local_state", persistence)
    return {
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "checks": checks,
        "ok": all(item["ok"] for item in checks.values()),
    }


def run(json_output: bool = False) -> int:
    result = diagnose()
    if json_output:
        print(json.dumps(result, indent=2))
    else:
        console = Console()
        console.print(Text(f"gdrivecopy {result['version']} · offline installation check"))
        table = Table("Check", "Result")
        for name, item in result["checks"].items():
            table.add_row(
                name.replace("_", " "),
                Text("OK" if item["ok"] else item["error"], style="green" if item["ok"] else "red"),
            )
        console.print(table)
        console.print("No Google account or live transfer was tested.")
    return 0 if result["ok"] else 1
