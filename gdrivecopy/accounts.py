"""Named OAuth profiles with explicit, server-verified account selection."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from gdrivecopy.auth import authenticate
from gdrivecopy.drive import DriveClient
from gdrivecopy.jobstore import JobLock
from gdrivecopy.persistence import write_text_atomic


def default_state_dir() -> Path:
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "gdrivecopy"
        )
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "gdrivecopy"


class Accounts:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "accounts.json"

    def read(self) -> dict:
        if not self.path.exists():
            return {"default": None, "profiles": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
            raise ValueError(
                "Invalid account registry; restore accounts.json from a trusted backup"
            )
        return data

    def add(self, name: str, credentials_path: Path) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise ValueError(
                "Account names use letters, digits, '-' and '_' (maximum 64 characters)"
            )
        with JobLock(self.directory / "accounts.lock"):
            registry = self.read()
            if name.casefold() in {existing.casefold() for existing in registry["profiles"]}:
                raise ValueError(f"Profile {name!r} already exists; choose a new profile name")
            token_path = self.directory / "accounts" / name / "token.json"
            if credentials_path.resolve().is_relative_to(self.directory.resolve()):
                raise ValueError(
                    "Keep the OAuth client JSON outside the application state directory"
                )
            # New named profiles always present the account chooser. Do not
            # silently reuse an unrelated current browser account.
            credentials = authenticate(credentials_path, token_path, select_account=True)
            identity = DriveClient(credentials).account_info()["user"]
            profile = {
                "email": identity["emailAddress"],
                "id": identity["permissionId"],
                "display_name": identity.get("displayName", ""),
                "credentials": str(credentials_path.resolve()),
            }
            registry["profiles"][name] = profile
            write_text_atomic(self.path, json.dumps(registry, indent=2))
            return profile

    def use(self, name: str) -> None:
        with JobLock(self.directory / "accounts.lock"):
            registry = self.read()
            if name not in registry["profiles"]:
                raise ValueError(f"Unknown account {name!r}; run 'gdrivecopy accounts list'")
            registry["default"] = name
            write_text_atomic(self.path, json.dumps(registry, indent=2))

    def select(self, name: str | None = None) -> tuple[str, dict]:
        registry = self.read()
        name = name or registry.get("default")
        if name is None and len(registry["profiles"]) == 1:
            name = next(iter(registry["profiles"]))
        if name is None or name not in registry["profiles"]:
            raise ValueError(
                "Select an account with --account NAME; run 'gdrivecopy accounts add NAME' first or 'accounts list'"
            )
        return name, registry["profiles"][name]

    def connect(self, name: str | None = None) -> tuple[str, dict, DriveClient]:
        name, profile = self.select(name)
        token_path = self.directory / "accounts" / name / "token.json"
        credentials = authenticate(Path(profile["credentials"]), token_path, select_account=True)
        drive = DriveClient(credentials)
        actual = drive.account_info()["user"]
        if actual["permissionId"] != profile["id"]:
            raise ValueError(
                f"Profile {name!r} authenticated a different Google account; create a separate named profile"
            )
        return name, {**profile, "email": actual["emailAddress"]}, drive
