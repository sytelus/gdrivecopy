"""Upload report formatting and persistence.

Produces both a human-readable summary for stdout and a JSON file for
programmatic consumption.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from gdrivecopy.models import UploadStats

logger = logging.getLogger(__name__)


def _fmt_bytes(n: int) -> str:
    """Format a byte count as a human-readable string (e.g. ``1.82 TB``)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n} B"  # unreachable


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
        f"  Files uploaded:     {stats.files_uploaded:>10,}  ({_fmt_bytes(stats.bytes_uploaded)})",
        f"  Files resumed:      {stats.files_resumed:>10,}",
        f"  Files skipped:      {stats.files_skipped:>10,}  (already on Drive)",
        f"  Size mismatches:    {stats.size_mismatches:>10,}  (see log for details)",
        f"  Files failed:       {stats.files_failed:>10,}",
        f"  Duration:           {_fmt_duration(stats.duration_seconds):>10}",
        f"  Daily limit hits:   {stats.daily_limit_hits:>10,}",
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
    return "\n".join(lines)


def save_report_json(stats: UploadStats, path: Path) -> None:
    """Write the report as a JSON file."""
    data = {
        "files_scanned": stats.files_scanned,
        "files_uploaded": stats.files_uploaded,
        "bytes_uploaded": stats.bytes_uploaded,
        "files_resumed": stats.files_resumed,
        "files_skipped": stats.files_skipped,
        "size_mismatches": stats.size_mismatches,
        "files_failed": stats.files_failed,
        "duration_seconds": round(stats.duration_seconds, 2),
        "daily_limit_hits": stats.daily_limit_hits,
        "errors": stats.errors,
        "mismatch_details": stats.mismatch_details,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Report written to %s", path)
