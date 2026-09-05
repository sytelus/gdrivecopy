"""Command-line interface for gdrivecopy.

Entry point is ``main()``, registered as the ``gdrivecopy`` console script
via ``pyproject.toml``.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import math
import sys
from pathlib import Path

from gdrivecopy import __version__
from gdrivecopy.commands import HelpParser, add_commands
from gdrivecopy.models import UploadConfig


def _parse_size(value: str) -> int:
    """Parse a human-readable size string like ``64M`` into bytes."""
    normalized = value.strip().upper()
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
    multiplier = 1
    for suffix, candidate in multipliers.items():
        if normalized.endswith(suffix):
            normalized = normalized[:-1]
            multiplier = candidate
            break
    try:
        numeric = float(normalized)
        if not math.isfinite(numeric):
            raise ValueError
        result = int(numeric * multiplier)
    except (OverflowError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid size: {value!r}") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("size must be greater than zero")
    return result


def _positive_int(value: str) -> int:
    """Parse a strictly positive integer for worker counts."""
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return result


def _nonempty(value: str) -> str:
    """Reject empty or whitespace-only identifiers."""
    result = value.strip()
    if not result:
        raise argparse.ArgumentTypeError("value must not be empty")
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = HelpParser(
        prog="gdrivecopy",
        description="Fast, resilient folder copies between your computer and Google Drive.",
        epilog="Start: gdrivecopy accounts add personal | Copy: gdrivecopy copy --help | Recover: gdrivecopy resume JOB_ID",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(
        dest="command", parser_class=HelpParser, title="Commands", metavar="COMMAND"
    )
    add_commands(sub, _parse_size, _positive_int)

    # -- upload ---------------------------------------------------------
    upload = sub.add_parser(
        "legacy-upload", help="Compatibility: v0.1 upload with JSON sessions (prefer copy)"
    )
    upload.add_argument("source_dir", type=Path, help="Local directory to upload")
    upload.add_argument("drive_folder_id", type=_nonempty, help="Google Drive folder ID (target)")

    upload.add_argument(
        "--transfers",
        type=_positive_int,
        default=4,
        help="Number of concurrent uploads (default: 4)",
    )
    upload.add_argument(
        "--chunk-size",
        type=_parse_size,
        default=64 * 1024 * 1024,
        help="Upload chunk size (default: 64M, must be multiple of 256K)",
    )
    upload.add_argument(
        "--bwlimit",
        type=_parse_size,
        default=None,
        help="Maximum upload speed, e.g. 50M (default: unlimited)",
    )
    upload.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan only, report what would be uploaded",
    )
    upload.add_argument(
        "--log-dir",
        type=Path,
        default=Path("."),
        help="Directory for log files (default: current directory)",
    )
    upload.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (default: INFO)",
    )
    upload.add_argument(
        "--credentials",
        type=Path,
        default=Path("credentials.json"),
        help="Path to OAuth credentials JSON (default: ./credentials.json)",
    )
    upload.add_argument(
        "--token",
        type=Path,
        default=Path("token.json"),
        help="Path to cached OAuth token (default: ./token.json)",
    )
    upload.add_argument(
        "--sessions",
        type=Path,
        default=Path("sessions.json"),
        help="Path to session cache file (default: ./sessions.json)",
    )
    upload.add_argument(
        "--no-checksum-verify",
        action="store_true",
        help="Skip post-upload MD5 verification",
    )
    upload.add_argument(
        "--quiet",
        action="store_true",
        help="Minimal output",
    )

    # -- auth -----------------------------------------------------------
    auth = sub.add_parser("auth", help="Compatibility: cache a v0.1 token (prefer accounts add)")
    auth.add_argument(
        "--credentials",
        type=Path,
        default=Path("credentials.json"),
        help="Path to OAuth credentials JSON",
    )
    auth.add_argument(
        "--token",
        type=Path,
        default=Path("token.json"),
        help="Path to store the cached OAuth token",
    )

    return parser


def _setup_logging(log_dir: Path, log_level: str, quiet: bool) -> Path:
    """Configure root logger with file and (optionally) console handlers."""
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_file = log_dir / f"gdrivecopy_{ts}.log"

    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    if not quiet:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(
        level=getattr(logging, log_level),
        format=fmt,
        handlers=handlers,
        force=True,
    )
    logging.getLogger(__name__).info("Log file: %s", log_file)
    return log_file.resolve()


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    try:
        _main(argv)
    except KeyboardInterrupt:
        print("gdrivecopy: cancelled", file=sys.stderr)
        sys.exit(130)
    except OSError as exc:
        # Includes log setup, token persistence and final report failures,
        # which can happen outside the upload exception boundary below.
        logging.getLogger(__name__).error("Filesystem operation failed: %s", exc)
        print(f"gdrivecopy: filesystem operation failed: {exc}", file=sys.stderr)
        sys.exit(1)


def _validate_state_paths(parser: argparse.ArgumentParser, paths: list[Path]) -> None:
    """Reject overlapping runtime roles before any file can be overwritten."""
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        parser.error("credentials, token, sessions, and report must use different paths")
    if any(path in other.parents for path in resolved for other in resolved if path != other):
        parser.error("a runtime file path cannot also be another runtime file's directory")


def _main(argv: list[str] | None = None) -> None:
    """Validate arguments, wire the services, and print the run outcome."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command in {
        "copy",
        "upload",
        "download",
        "resume",
        "accounts",
        "jobs",
        "report",
        "doctor",
    }:
        from sqlite3 import Error as SQLiteError

        from google.auth.exceptions import GoogleAuthError
        from googleapiclient.errors import HttpError
        from requests.exceptions import RequestException

        from gdrivecopy.commands import execute
        from gdrivecopy.drive import DriveApiError

        try:
            code = execute(args)
        except (
            ValueError,
            RuntimeError,
            GoogleAuthError,
            HttpError,
            DriveApiError,
            RequestException,
            SQLiteError,
        ) as exc:
            print(f"gdrivecopy: {exc}", file=sys.stderr)
            sys.exit(1)
        if code:
            sys.exit(code)
        return

    if args.command == "auth":
        _validate_state_paths(parser, [args.credentials, args.token])
        _setup_logging(Path("."), "INFO", quiet=False)
        from google.auth.exceptions import GoogleAuthError

        from gdrivecopy.auth import authenticate

        try:
            authenticate(args.credentials, args.token)
        except (FileNotFoundError, GoogleAuthError, ValueError) as exc:
            logging.getLogger(__name__).error("Authentication failed: %s", exc)
            sys.exit(1)
        print(f"Authentication successful. Token saved to {args.token}")
        return

    if args.command == "legacy-upload":
        chunk_size = args.chunk_size
        if chunk_size < 256 * 1024 or chunk_size % (256 * 1024) != 0:
            parser.error("--chunk-size must be a multiple of 256K (minimum 256K)")

        source_dir = args.source_dir.resolve()
        if not source_dir.is_dir():
            parser.error(f"Source directory does not exist: {source_dir}")

        _validate_state_paths(
            parser, [args.credentials, args.token, args.sessions, args.log_dir / "report.json"]
        )

        log_path = _setup_logging(args.log_dir, args.log_level, args.quiet)

        config = UploadConfig(
            source_dir=source_dir,
            drive_folder_id=args.drive_folder_id,
            transfers=args.transfers,
            chunk_size=chunk_size,
            bwlimit=args.bwlimit,
            dry_run=args.dry_run,
            credentials_path=args.credentials,
            token_path=args.token,
            session_path=args.sessions,
            verify_checksum=not args.no_checksum_verify,
            quiet=args.quiet,
            log_dir=args.log_dir,
            log_path=log_path,
            log_level=args.log_level,
        )

        from google.auth.exceptions import GoogleAuthError
        from googleapiclient.errors import HttpError
        from httplib2 import HttpLib2Error
        from requests.exceptions import RequestException

        from gdrivecopy.auth import authenticate
        from gdrivecopy.drive import DriveApiError, DriveClient
        from gdrivecopy.report import format_report, save_report_json
        from gdrivecopy.uploader import Uploader

        try:
            creds = authenticate(config.credentials_path, config.token_path)
            drive = DriveClient(creds)
            uploader = Uploader(config, drive)
            stats = uploader.run()
        except KeyboardInterrupt:
            logging.getLogger(__name__).warning("Upload interrupted by user")
            if args.quiet:
                print("gdrivecopy: upload interrupted by user", file=sys.stderr)
            sys.exit(130)
        except (
            DriveApiError,
            FileNotFoundError,
            GoogleAuthError,
            HttpError,
            HttpLib2Error,
            RequestException,
            ValueError,
        ) as exc:
            logging.getLogger(__name__).error("Upload aborted: %s", exc)
            if args.quiet:
                print(f"gdrivecopy: upload aborted: {exc}", file=sys.stderr)
            sys.exit(1)

        report_text = format_report(stats)
        print(report_text)

        report_path = config.log_dir / "report.json"
        save_report_json(stats, report_path)

        if (
            stats.files_failed > 0
            or stats.scan_errors > 0
            or stats.size_mismatches > 0
            or stats.path_conflicts > 0
            or stats.quota_limit_hits > 0
        ):
            sys.exit(1)


if __name__ == "__main__":
    main()
