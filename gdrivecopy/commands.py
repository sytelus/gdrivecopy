"""Modern copy, resume, account, job, and report command surfaces."""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from gdrivecopy.accounts import Accounts, default_state_dir
from gdrivecopy.drive import DriveClient
from gdrivecopy.jobstore import JobLock, JobStore, utc_now
from gdrivecopy.redaction import protect_logs, safe_error
from gdrivecopy.terminal import Dashboard, render_report
from gdrivecopy.transfer import TransferRunner, build_report

JOB_PATTERN = re.compile(r"^\d{8}-\d{6}-[a-f0-9]{8}$")


class HelpParser(argparse.ArgumentParser):
    """Readable colored help on terminals, ordinary text when redirected."""

    def print_help(self, file=None):
        text = Text(self.format_help())
        text.highlight_regex(r"--[a-z][a-z-]*", "bold cyan")
        text.highlight_regex(r"(?m)^[A-Za-z][^\n]*:$", "bold bright_cyan")
        Console(file=file, highlight=False).print(text, end="")


def add_commands(sub, parse_size, positive_int) -> None:
    def state(parser):
        parser.add_argument(
            "--state-dir",
            type=Path,
            default=default_state_dir(),
            help="Account and job storage (default: per-user application state)",
        )

    def options(parser):
        state(parser)
        parser.add_argument(
            "--account", help="Named Google account; always displayed before copying"
        )
        parser.add_argument(
            "--transfers",
            type=positive_int,
            default=4,
            metavar="N",
            help="Parallel files (default: 4)",
        )
        parser.add_argument(
            "--chunk-size",
            type=parse_size,
            default=64 * 1024 * 1024,
            metavar="SIZE",
            help="Bounded transfer buffer per worker (default: 64M)",
        )
        parser.add_argument(
            "--bwlimit", type=parse_size, metavar="RATE", help="Aggregate payload rate, e.g. 50M"
        )
        parser.add_argument(
            "--retries",
            type=positive_int,
            default=8,
            metavar="N",
            help="Attempts per upload / download range (default: 8)",
        )
        parser.add_argument(
            "--existing",
            choices=["size", "checksum"],
            default="size",
            help="Existing-file comparison: size is fast; checksum reads local bytes",
        )
        parser.add_argument(
            "--verification",
            choices=["checksum", "size"],
            default="checksum",
            help="New copies: checksum verified by default; size weakens integrity",
        )
        parser.add_argument(
            "--exclude",
            action="append",
            default=[],
            metavar="GLOB",
            help="Exclude relative paths matching GLOB; repeatable",
        )
        parser.add_argument(
            "--export-docs",
            choices=["office", "pdf"],
            help="Export native Docs/Sheets/Slides/Drawings; conversions are reported separately",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Plan and report without copying file content"
        )
        parser.add_argument(
            "--no-progress", action="store_true", help="Plain logging instead of a live dashboard"
        )
        parser.add_argument(
            "--quiet", action="store_true", help="Only final report and fatal errors"
        )
        parser.add_argument(
            "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO"
        )

    copy = sub.add_parser(
        "copy",
        help="Copy folder contents to or from Google Drive",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Copy SOURCE folder contents into DESTINATION. Exactly one side must be drive:FOLDER_ID.",
        epilog=r"""Examples:
  gdrivecopy copy "D:\Photos" drive:FOLDER_ID --account personal
  gdrivecopy copy drive:root "E:\Drive backup" --account work
  gdrivecopy copy drive:FOLDER_ID "E:\Photos" "*.jpg" "*.png"
  gdrivecopy copy "D:\Archive" drive:FOLDER_ID --dry-run
  gdrivecopy copy drive:FOLDER_ID "E:\Backup" --existing checksum

Folders are recursive. Existing content is never overwritten or deleted.
Ctrl+C checkpoints the job. Use the printed resume command after a reboot.
Fast same-size skips are reported separately from checksum-verified copies.""",
    )
    copy.add_argument("source", metavar="SOURCE", help="Local folder or drive:FOLDER_ID")
    copy.add_argument(
        "destination", metavar="DESTINATION", help="Destination folder; exactly one side is Drive"
    )
    copy.add_argument(
        "patterns",
        nargs="*",
        metavar="FILE_PATTERN",
        help="Optional filename patterns (default: all files)",
    )
    options(copy)
    download = sub.add_parser("download", help="Download alias: download FOLDER_ID LOCAL_DIR")
    download.add_argument("source")
    download.add_argument("destination")
    download.set_defaults(patterns=[])
    options(download)
    upload = sub.add_parser("upload", help="Upload alias: upload LOCAL_DIR FOLDER_ID")
    upload.add_argument("source")
    upload.add_argument("destination")
    upload.set_defaults(patterns=[])
    options(upload)

    resume = sub.add_parser(
        "resume", help="Continue an existing job after cancellation, a crash, or reboot"
    )
    resume.add_argument("job_id", help="Job ID printed by copy or jobs")
    state(resume)
    resume.add_argument(
        "--account", help="Account override; must identify the original Google user"
    )
    resume.add_argument("--transfers", type=positive_int, metavar="N")
    resume.add_argument("--bwlimit", type=parse_size, metavar="RATE")
    resume.add_argument(
        "--dry-run", action="store_true", help="Reconcile and preview without transferring"
    )
    resume.add_argument("--no-progress", action="store_true")
    resume.add_argument("--quiet", action="store_true")
    resume.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO"
    )

    accounts = sub.add_parser("accounts", help="Add, list, or choose a named Google account")
    actions = accounts.add_subparsers(dest="account_action", required=True, parser_class=HelpParser)
    add = actions.add_parser("add", help="Sign in to a new named account profile")
    add.add_argument("name")
    add.add_argument("--credentials", type=Path, default=Path("credentials.json"))
    state(add)
    listing = actions.add_parser(
        "list", help="Show account names, verified email addresses, and default"
    )
    state(listing)
    use = actions.add_parser("use", help="Explicitly set the default account")
    use.add_argument("name")
    state(use)
    jobs = sub.add_parser("jobs", help="List saved jobs and their current status")
    state(jobs)
    report = sub.add_parser("report", help="Read a job's report or export its audit trail")
    report.add_argument("job_id")
    state(report)
    report.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    report.add_argument("--audit-output", type=Path, help="Export the full audit as JSON Lines")
    doctor = sub.add_parser("doctor", help="Check this installation offline, without signing in")
    doctor.add_argument("--json", action="store_true", help="Print diagnostics as JSON")


def remote_id(value: str) -> str | None:
    if value.startswith("drive:"):
        identifier = value[6:].removeprefix("//")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
            raise ValueError("Use drive:FOLDER_ID or drive:root")
        return identifier
    return None


def job_directory(state: Path, identifier: str) -> Path:
    if not JOB_PATTERN.fullmatch(identifier):
        raise ValueError("Invalid job ID; use 'gdrivecopy jobs' to find saved jobs")
    directory = state / "jobs" / identifier
    if not (directory / "job.sqlite3").is_file():
        raise ValueError(f"Job {identifier} was not found in {state}")
    return directory


def configure_logging(directory: Path, args, dashboard: bool) -> None:
    handler = RotatingFileHandler(
        directory / "run.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handlers = [handler]
    if not dashboard and not args.quiet:
        plain = logging.StreamHandler()
        handlers.append(plain)
    protect_logs(handlers)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def execute(args) -> int:
    if args.command == "doctor":
        from gdrivecopy.diagnostics import run

        return run(args.json)
    state = args.state_dir.resolve()
    if args.command == "accounts":
        accounts = Accounts(state)
        if args.account_action == "add":
            profile = accounts.add(args.name, args.credentials)
            Console().print(
                Text(
                    f"Added {args.name}: {profile['email']}\nUse --account {args.name}, or run 'gdrivecopy accounts use {args.name}'."
                )
            )
        elif args.account_action == "use":
            accounts.use(args.name)
            Console().print(Text(f"Default account: {args.name}"))
        else:
            registry = accounts.read()
            table = Table("Profile", "Google account", "Default")
            for name, profile in registry["profiles"].items():
                table.add_row(
                    name, Text(profile["email"]), "✓" if registry["default"] == name else ""
                )
            Console().print(table)
        return 0
    if args.command == "jobs":
        table = Table("Job", "Status", "Direction", "Account")
        failed = False
        for path in sorted((state / "jobs").glob("*/job.sqlite3"), reverse=True):
            try:
                # SQLite's own context manager commits/rolls back; it does not
                # close the connection. Explicit closure avoids leaked handles.
                with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
                    values = {
                        key: json.loads(value)
                        for key, value in db.execute(
                            "SELECT key,value FROM meta WHERE key IN ('config','status')"
                        )
                    }
                config = values.get("config", {})
                if not isinstance(config, dict) or not isinstance(values.get("status", "new"), str):
                    raise ValueError("Invalid job summary")
            except (OSError, sqlite3.Error, ValueError) as exc:
                failed = True
                table.add_row(Text(path.parent.name), "Unreadable", "", "")
                Console(stderr=True).print(Text(f"{path.parent.name}: {safe_error(exc)}"))
                continue
            table.add_row(
                path.parent.name,
                values.get("status", "new"),
                config.get("direction", ""),
                Text(config.get("account_email", "")),
            )
        Console().print(table)
        return 1 if failed else 0
    if args.command == "report":
        directory = job_directory(state, args.job_id)
        store = JobStore(directory, readonly=True)
        try:
            report = build_report(store)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                render_report(report)
            if args.audit_output:
                store.export_audit(args.audit_output)
        finally:
            store.close()
        return 0

    if args.command == "resume":
        directory = job_directory(state, args.job_id)
        with JobLock(directory / "run.lock"):
            store = JobStore(directory)
            try:
                config = store.get("config")
                name, identity, drive = Accounts(state).connect(args.account or config["account"])
                if identity["id"] != config["account_id"]:
                    raise ValueError("The selected Google account does not own this job")
                config.update(account=name, account_email=identity["email"], state_dir=str(state))
                config["dry_run"] = args.dry_run
                if args.transfers:
                    config["transfers"] = args.transfers
                if args.bwlimit:
                    config["bwlimit"] = args.bwlimit
                store.set("config", config)
                return run_connected(store, drive, config, args)
            finally:
                store.close()

    source = args.source
    destination = args.destination
    if args.command == "download" and not source.startswith("drive:"):
        source = "drive:" + source
    if args.command == "upload" and not destination.startswith("drive:"):
        destination = "drive:" + destination
    src_remote, dst_remote = remote_id(source), remote_id(destination)
    if (src_remote is None) == (dst_remote is None):
        raise ValueError(
            "Exactly one side must be drive:FOLDER_ID; local-to-local and Drive-to-Drive copies are not supported"
        )
    direction = "download" if src_remote else "upload"
    local = Path(destination if src_remote else source).resolve()
    if direction == "upload" and not local.is_dir():
        raise ValueError(f"Source directory does not exist: {local}")
    if local.is_relative_to(state):
        raise ValueError(
            "Source/destination must be outside the private application state directory"
        )
    if direction == "download" and local.exists() and not local.is_dir():
        raise ValueError("The local destination must be a directory")
    if args.chunk_size < 256 * 1024 or args.chunk_size % (256 * 1024):
        raise ValueError("--chunk-size must be at least 256K and a multiple of 256K")
    account, identity, drive = Accounts(state).connect(args.account)
    identifier = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    config = {
        "schema_version": 1,
        "job_id": identifier,
        "direction": direction,
        "source": source,
        "destination": destination,
        "local_path": str(local),
        "remote_root": src_remote or dst_remote,
        "state_dir": str(state),
        "account": account,
        "account_id": identity["id"],
        "account_email": identity["email"],
        "transfers": args.transfers,
        "chunk_size": args.chunk_size,
        "bwlimit": args.bwlimit,
        "retries": args.retries,
        "verification": args.verification,
        "existing": args.existing,
        "patterns": args.patterns,
        "exclude": args.exclude,
        "dry_run": args.dry_run,
    }
    config["export_docs"] = args.export_docs
    config["excluded_files"] = [
        identity["credentials"],
        *(str(local / name) for name in ("credentials.json", "token.json", "sessions.json")),
    ]
    directory = state / "jobs" / identifier
    with JobLock(directory / "run.lock"):
        store = JobStore(directory)
        try:
            store.set("config", config)
            store.set("created", utc_now())
            return run_connected(store, drive, config, args)
        finally:
            store.close()


def run_connected(store: JobStore, drive: DriveClient, config: dict, args) -> int:
    console = Console(stderr=True)
    dashboard = not args.no_progress and not args.quiet and console.is_terminal
    configure_logging(store.directory, args, dashboard)
    if not args.quiet:
        verification = (
            "checksum verified"
            if config["verification"] == "checksum"
            else "SIZE ONLY (no checksum verification)"
        )
        console.print(
            Text(
                f"Account: {config['account_email']} ({config['account']})\nJob: {config['job_id']}\n"
                f"New copies: {verification}. Existing comparison: {config['existing']}."
            )
        )
    runner = TransferRunner(store, drive, config)
    previous = signal.getsignal(signal.SIGINT)

    def cancel(_signum, _frame):
        runner.control.cancel()
        runner.model.event(
            "phase", phase="Stopping safely · finishing active requests and saving checkpoints"
        )

    signal.signal(signal.SIGINT, cancel)
    try:
        with Dashboard(
            runner.model,
            account=config["account_email"],
            direction=config["direction"],
            job_id=config["job_id"],
            console=console,
            enabled=dashboard,
        ):
            report = runner.run()
    finally:
        signal.signal(signal.SIGINT, previous)
    render_report(report)
    if report["status"] == "cancelled":
        return 130
    return 0 if report["status"] in {"complete", "planned"} else 1
