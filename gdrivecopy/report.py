"""Upload report formatting and persistence.

Produces both a human-readable summary for stdout and a JSON file for
programmatic consumption.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from gdrivecopy.models import UploadStats
from gdrivecopy.persistence import write_text_atomic
from gdrivecopy.redaction import safe_error

logger = logging.getLogger(__name__)


def _fmt_bytes(n: int) -> str:
    """Format a byte count as a human-readable string (e.g. ``1.82 TB``)."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{n} {unit}"
        value /= 1024
    raise AssertionError("byte-formatting loop exited unexpectedly")


def _fmt_duration(seconds: float) -> str:
    """Format seconds as ``Xh Ym Zs``."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m:02d}m")
    parts.append(f"{s:02d}s")
    return " ".join(parts)


def format_report(stats: UploadStats) -> str:
    """Return a human-readable summary string."""
    lines = [
        "",
        "=" * 50,
        "  gdrivecopy - Upload Report",
        "=" * 50,
        f"  Files scanned:      {stats.files_scanned:>10,}",
        f"  Tool files excluded: {stats.files_excluded:>10,}",
        f"  Scan errors:        {stats.scan_errors:>10,}",
        f"  Files to upload:    {stats.files_to_upload:>10,}",
        f"  Files uploaded:     {stats.files_uploaded:>10,}  ({_fmt_bytes(stats.bytes_uploaded)})",
        f"  Files resumed:      {stats.files_resumed:>10,}",
        f"  Files skipped:      {stats.files_skipped:>10,}  (existing or conflicting paths)",
        f"  Symlinks skipped:   {stats.symlinks_skipped:>10,}",
        f"  Size mismatches:    {stats.size_mismatches:>10,}  (see log for details)",
        f"  Path conflicts:     {stats.path_conflicts:>10,}  (see errors)",
        f"  Files failed:       {stats.files_failed:>10,}",
        f"  Duration:           {_fmt_duration(stats.duration_seconds):>10}",
        f"  Blocking quotas:    {stats.quota_limit_hits:>10,}",
        "=" * 50,
    ]

    if stats.mismatch_details:
        lines.append("")
        lines.append("  Size mismatches:")
        for detail in stats.mismatch_details:
            lines.append(f"    - {detail}")

    if stats.errors:
        lines.append("")
        lines.append(f"  Errors ({len(stats.errors)}):")
        for err in stats.errors[:20]:
            lines.append(f"    - {err}")
        if len(stats.errors) > 20:
            lines.append(f"    ... and {len(stats.errors) - 20} more (see log)")

    lines.append("")
    return safe_error("\n".join(lines))


def save_report_json(stats: UploadStats, path: Path) -> None:
    """Write the report as a JSON file atomically."""
    data = {
        "files_scanned": stats.files_scanned,
        "files_excluded": stats.files_excluded,
        "scan_errors": stats.scan_errors,
        "symlinks_skipped": stats.symlinks_skipped,
        "files_to_upload": stats.files_to_upload,
        "files_uploaded": stats.files_uploaded,
        "bytes_uploaded": stats.bytes_uploaded,
        "files_resumed": stats.files_resumed,
        "files_skipped": stats.files_skipped,
        "size_mismatches": stats.size_mismatches,
        "path_conflicts": stats.path_conflicts,
        "files_failed": stats.files_failed,
        "duration_seconds": round(stats.duration_seconds, 2),
        "quota_limit_hits": stats.quota_limit_hits,
        "errors": [safe_error(error) for error in stats.errors],
        "mismatch_details": stats.mismatch_details,
    }
    write_text_atomic(path, json.dumps(data, indent=2))
    logger.info("Report written to %s", path)
