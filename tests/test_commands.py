"""Account isolation, modern CLI wiring, and terminal rendering."""

import io
import json
import sqlite3
from unittest.mock import patch

import pytest
from rich.console import Console

from gdrivecopy.accounts import Accounts
from gdrivecopy.cli import _build_parser, main
from gdrivecopy.commands import execute
from gdrivecopy.control import ProgressModel
from gdrivecopy.jobstore import JobStore
from gdrivecopy.terminal import Dashboard
from tests.fake_drive import FakeDrive


def registry(tmp_path, profiles, default=None):
    profiles = {
        name: {"id": name, "email": f"{name}@example.test", "credentials": "client.json", **profile}
        for name, profile in profiles.items()
    }
    (tmp_path / "accounts.json").write_text(json.dumps({"profiles": profiles, "default": default}))
    return Accounts(tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        {"profiles": {"../escape": {}}},
        {"profiles": {"one": None}},
        {"profiles": {"one": {"id": 12, "email": "a", "credentials": "b"}}},
        {"profiles": {}, "default": []},
        {"profiles": {}, "default": "missing"},
    ],
)
def test_invalid_account_registry_fails_before_authentication(tmp_path, payload):
    (tmp_path / "accounts.json").write_text(json.dumps(payload))
    with (
        patch("gdrivecopy.accounts.authenticate") as auth,
        pytest.raises(ValueError, match="Invalid"),
    ):
        Accounts(tmp_path).connect()
    auth.assert_not_called()


def test_jobs_shows_healthy_jobs_and_closes_connections_when_one_is_corrupt(tmp_path, capsys):
    good = JobStore(tmp_path / "jobs" / "20260904-120000-1234abcd")
    good.set("config", {"direction": "download", "account_email": "user@example.test"})
    good.set("status", "complete")
    good.close()
    bad = tmp_path / "jobs" / "20260904-130000-1234abcd"
    bad.mkdir()
    (bad / "job.sqlite3").write_bytes(b"not a database")
    connections = []
    connect = sqlite3.connect

    def tracked(*args, **kwargs):
        db = connect(*args, **kwargs)
        connections.append(db)
        return db

    args = _build_parser().parse_args(["jobs", "--state-dir", str(tmp_path)])
    with patch("gdrivecopy.commands.sqlite3.connect", tracked):
        assert execute(args) == 1
    output = capsys.readouterr()
    assert "Unreadable" in output.out and "complete" in output.out
    assert "not a database" in output.err
    assert len(connections) == 2
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")


def test_multiple_accounts_need_explicit_choice(tmp_path):
    accounts = registry(tmp_path, {"one": {}, "two": {}})
    with pytest.raises(ValueError, match="Select an account"):
        accounts.select()
    accounts.use("two")
    assert accounts.select()[0] == "two"
    assert accounts.select("one")[0] == "one"


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_copy_cannot_use_private_state_as_payload(tmp_path, direction):
    state = tmp_path / "state"
    state.mkdir()
    args = _build_parser().parse_args(
        [
            direction,
            str(state) if direction == "upload" else "root",
            "root" if direction == "upload" else str(state),
            "--state-dir",
            str(state),
        ]
    )
    with (
        patch("gdrivecopy.commands.Accounts.connect") as connect,
        pytest.raises(ValueError, match="private application state"),
    ):
        execute(args)
    connect.assert_not_called()


def test_credentials_cannot_switch_identity_silently(tmp_path):
    accounts = registry(
        tmp_path,
        {"one": {"id": "original", "email": "one@example.test", "credentials": "client.json"}},
    )
    with (
        patch("gdrivecopy.accounts.authenticate"),
        patch("gdrivecopy.accounts.DriveClient") as drive,
    ):
        drive.return_value.account_info.return_value = {
            "user": {"permissionId": "other", "emailAddress": "other@example.test"}
        }
        with pytest.raises(ValueError, match="different Google account"):
            accounts.connect("one")


def test_profile_names_cannot_alias_on_windows(tmp_path):
    accounts = registry(tmp_path, {"Personal": {}})
    with (
        patch("gdrivecopy.accounts.authenticate") as auth,
        pytest.raises(ValueError, match="already exists"),
    ):
        accounts.add("personal", tmp_path / "client.json")
    auth.assert_not_called()


@pytest.mark.parametrize(
    "command", ["copy", "upload", "download", "resume", "accounts", "jobs", "report"]
)
def test_help_never_authenticates(command, capsys):
    with patch("gdrivecopy.accounts.authenticate") as auth, pytest.raises(SystemExit) as exc:
        main([command, "--help"])
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out
    auth.assert_not_called()


@pytest.mark.parametrize("command", ["copy", "upload", "download"])
def test_copy_aliases_use_durable_runner(command, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    state = tmp_path / "state"
    source, destination = (
        ("drive:root", str(local)) if command == "download" else (str(local), "drive:root")
    )
    args = _build_parser().parse_args(
        [command, source, destination, "--state-dir", str(state), "--quiet"]
    )
    identity = {
        "id": "user",
        "email": "user@example.test",
        "credentials": str(tmp_path / "client.json"),
    }
    with (
        patch(
            "gdrivecopy.commands.Accounts.connect", return_value=("personal", identity, FakeDrive())
        ),
        patch("gdrivecopy.commands.run_connected", return_value=0) as run,
    ):
        assert execute(args) == 0
    config = run.call_args.args[2]
    assert config["direction"] == ("download" if command == "download" else "upload")
    assert config["account_email"] == "user@example.test"
    assert list((state / "jobs").glob("*/job.sqlite3"))


def test_resume_refuses_different_owner(tmp_path):
    identifier = "20260904-120000-1234abcd"
    store = JobStore(tmp_path / "jobs" / identifier)
    store.set("config", {"account": "personal", "account_id": "original"})
    store.close()
    args = _build_parser().parse_args(["resume", identifier, "--state-dir", str(tmp_path)])
    with (
        patch(
            "gdrivecopy.commands.Accounts.connect",
            return_value=("personal", {"id": "other"}, FakeDrive()),
        ),
        patch("gdrivecopy.commands.run_connected") as run,
        pytest.raises(ValueError, match="does not own"),
    ):
        execute(args)
    run.assert_not_called()


def test_report_connection_is_read_only(tmp_path):
    store = JobStore(tmp_path)
    store.set("hello", "world")
    store.close()
    store = JobStore(tmp_path, readonly=True)
    assert store.get("hello") == "world"
    with pytest.raises(sqlite3.OperationalError):
        store.set("hello", "changed")
    store.close()


def test_cli_cancel_resume_and_audit_without_live_google(tmp_path, capsys):
    state, local = tmp_path / "state", tmp_path / "download"
    drive = FakeDrive()
    drive.add_file("file.bin", b"x" * 700_000)
    drive.cancel_after_range = True
    identity = {
        "id": "user",
        "email": "user@example.test",
        "credentials": str(tmp_path / "client.json"),
    }
    with (
        patch("gdrivecopy.commands.Accounts.connect", return_value=("personal", identity, drive)),
        patch("gdrivecopy.commands.configure_logging"),
    ):
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "copy",
                    "drive:root",
                    str(local),
                    "--state-dir",
                    str(state),
                    "--chunk-size",
                    "256K",
                    "--no-progress",
                ]
            )
        assert exc.value.code == 130
        identifier = next((state / "jobs").iterdir()).name
        main(["resume", identifier, "--state-dir", str(state), "--no-progress"])
    output = capsys.readouterr()
    assert "CANCELLED" in output.out and "COMPLETE" in output.out
    assert "user@example.test" in output.err
    assert (local / "file.bin").stat().st_size == 700_000
    audit = tmp_path / "audit.jsonl"
    main(["report", identifier, "--state-dir", str(state), "--json", "--audit-output", str(audit)])
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "complete"
    assert report["counts"]["copied"]["files"] == 1
    assert all(json.loads(line)["kind"] for line in audit.read_text().splitlines())
    assert (state / "jobs" / identifier / "files.csv").exists()


def test_account_add_uses_account_chooser_and_verified_identity(tmp_path):
    accounts = Accounts(tmp_path / "state")
    with (
        patch("gdrivecopy.accounts.authenticate") as auth,
        patch("gdrivecopy.accounts.DriveClient") as drive,
    ):
        drive.return_value.account_info.return_value = {
            "user": {"emailAddress": "chosen@example.test", "permissionId": "chosen"}
        }
        profile = accounts.add("personal", tmp_path / "client.json")
    assert auth.call_args.kwargs["select_account"] is True
    assert profile["id"] == "chosen"
    assert accounts.select()[0] == "personal"


@pytest.mark.parametrize("width", [40, 80, 120])
def test_dashboard_has_bounded_lines_and_literal_paths(width):
    model = ProgressModel()
    model.event("plan", files=100, bytes=10**13)
    model.event("start", "[red]literal filename[/red]" + "x" * 100, size=10**12)
    model.event("progress", "[red]literal filename[/red]" + "x" * 100, offset=1000, size=10**12)
    stream = io.StringIO()
    console = Console(file=stream, width=width, height=30, color_system=None)
    dashboard = Dashboard(
        model, account="user@example.test", direction="download", job_id="example", console=console
    )
    console.print(dashboard.render())
    output = stream.getvalue()
    assert "gdrivecopy" in output
    assert all(len(line) <= width for line in output.splitlines())
    assert len(output.splitlines()) < 45
