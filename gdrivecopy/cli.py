"""Command-line interface for gdrivecopy.

Entry point is ``main()``, registered as the ``gdrivecopy`` console script
via ``pyproject.toml``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from gdrivecopy.models import UploadConfig


def _parse_size(value: str) -> int:
    """Parse a human-readable size string like ``64M`` into bytes."""
    value = value.strip().upper()
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
    for suffix, mult in multipliers.items():
        if value.endswith(suffix):
            return int(float(value[:-1]) * mult)
    return int(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gdrivecopy",
        description="Fast, resilient bulk upload utility for Google Drive.",
    )
    sub = parser.add_subparsers(dest="command")

    # -- upload ---------------------------------------------------------
    upload = sub.add_parser("upload", help="Upload files to Google Drive")
    upload.add_argument("source_dir", type=Path, help="Local directory to upload")
    upload.add_argument("drive_folder_id", help="Google Drive folder ID (target)")

    upload.add_argument(
        "--transfers", type=int, default=4,
        help="Number of concurrent uploads (default: 4)",
    )
    upload.add_argument(
        "--chunk-size", type=str, default="64M",
        help="Upload chunk size (default: 64M, must be multiple of 256K)",
    )
    upload.add_argument(
        "--bwlimit", type=str, default=None,
        help="Maximum upload speed, e.g. 50M (default: unlimited)",
    )
    upload.add_argument(
        "--dry-run", action="store_true",
        help="Scan only, report what would be uploaded",
    )
    upload.add_argument(
        "--log-dir", type=Path, default=Path("."),
        help="Directory for log files (default: current directory)",
    )
    upload.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (default: INFO)",
    )
    upload.add_argument(
        "--credentials", type=Path, default=Path("credentials.json"),
        help="Path to OAuth credentials JSON (default: ./credentials.json)",
    )
    upload.add_argument(
        "--token", type=Path, default=Path("token.json"),
        help="Path to cached OAuth token (default: ./token.json)",
    )
    upload.add_argument(
        "--sessions", type=Path, default=Path("sessions.json"),
        help="Path to session cache file (default: ./sessions.json)",
    )
    upload.add_argument(
        "--no-checksum-verify", action="store_true",
        help="Skip post-upload MD5 verification",
    )
    upload.add_argument(
        "--wait-on-limit", action="store_true",
        help="Wait and auto-resume when daily limit is hit (default: exit)",
    )
    upload.add_argument(
        "--quiet", action="store_true",
        help="Minimal output",
    )

    # -- auth -----------------------------------------------------------
    auth = sub.add_parser("auth", help="Run OAuth flow and cache token")
    auth.add_argument(
        "--credentials", type=Path, default=Path("credentials.json"),
        help="Path to OAuth credentials JSON",
    )
    auth.add_argument(
        "--token", type=Path, default=Path("token.json"),
        help="Path to store the cached OAuth token",
    )

    return parser


def _setup_logging(log_dir: Path, log_level: str, quiet: bool) -> None:
    """Configure root logger with file and (optionally) console handlers."""
    import datetime

    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
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
    )
    logging.getLogger(__name__).info("Log file: %s", log_file)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "auth":
        _setup_logging(Path("."), "INFO", quiet=False)
        from gdrivecopy.auth import authenticate

        authenticate(args.credentials, args.token)
        print(f"Authentication successful. Token saved to {args.token}")
        return

    if args.command == "upload":
        chunk_size = _parse_size(args.chunk_size)
        if chunk_size < 256 * 1024 or chunk_size % (256 * 1024) != 0:
            parser.error("--chunk-size must be a multiple of 256K (minimum 256K)")

        bwlimit = _parse_size(args.bwlimit) if args.bwlimit else None

        _setup_logging(args.log_dir, args.log_level, args.quiet)

        config = UploadConfig(
            source_dir=args.source_dir.resolve(),
            drive_folder_id=args.drive_folder_id,
            transfers=args.transfers,
            chunk_size=chunk_size,
            bwlimit=bwlimit,
            dry_run=args.dry_run,
            credentials_path=args.credentials,
            token_path=args.token,
            session_path=args.sessions,
            verify_checksum=not args.no_checksum_verify,
            wait_on_limit=args.wait_on_limit,
            quiet=args.quiet,
            log_dir=args.log_dir,
            log_level=args.log_level,
        )

        if not config.source_dir.is_dir():
            parser.error(f"Source directory does not exist: {config.source_dir}")

        from gdrivecopy.auth import authenticate
        from gdrivecopy.drive import DriveClient
        from gdrivecopy.report import format_report, save_report_json
        from gdrivecopy.uploader import Uploader

        creds = authenticate(config.credentials_path, config.token_path)
        drive = DriveClient(creds)
        uploader = Uploader(config, drive)
        stats = uploader.run()

        report_text = format_report(stats)
        print(report_text)

        report_path = config.log_dir / "report.json"
        save_report_json(stats, report_path)

        if stats.files_failed > 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
