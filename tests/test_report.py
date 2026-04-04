"""Tests for gdrivecopy.report -- upload report formatting and persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gdrivecopy.models import UploadStats
from gdrivecopy.report import _fmt_bytes, _fmt_duration, format_report, save_report_json


# ---------------------------------------------------------------------------
# _fmt_bytes
# ---------------------------------------------------------------------------


class TestFmtBytes:
    def test_zero_bytes(self) -> None:
        """Zero bytes should display as '0 B'."""
        assert _fmt_bytes(0) == "0 B"

    def test_bytes_below_1kb(self) -> None:
        """Values under 1024 display in bytes."""
        assert _fmt_bytes(512) == "512 B"

    def test_kilobytes(self) -> None:
        """1024 bytes is exactly 1.00 KB."""
        assert _fmt_bytes(1024) == "1.00 KB"

    def test_megabytes(self) -> None:
        """1 MiB formats as MB."""
        result = _fmt_bytes(1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self) -> None:
        """1 GiB formats as GB."""
        result = _fmt_bytes(1024**3)
        assert "GB" in result

    def test_terabytes(self) -> None:
        """1 TiB formats as TB."""
        result = _fmt_bytes(1024**4)
        assert "TB" in result


# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_zero_seconds(self) -> None:
        """Zero duration is '00s'."""
        assert _fmt_duration(0) == "00s"

    def test_seconds_only(self) -> None:
        """Under a minute: just seconds."""
        assert _fmt_duration(45) == "45s"

    def test_minutes_and_seconds(self) -> None:
        """90 seconds is 1 minute 30 seconds."""
        result = _fmt_duration(90)
        assert "01m" in result
        assert "30s" in result

    def test_hours_minutes_seconds(self) -> None:
        """3661 seconds is 1h 01m 01s."""
        result = _fmt_duration(3661)
        assert "1h" in result
        assert "01m" in result
        assert "01s" in result


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


class TestFormatReport:
    def test_contains_all_counters(self) -> None:
        """The formatted report includes all key statistics."""
        stats = UploadStats(
            files_scanned=100,
            files_uploaded=80,
            bytes_uploaded=1024 * 1024 * 500,
            files_resumed=5,
            files_skipped=10,
            size_mismatches=2,
            files_failed=3,
            duration_seconds=125.7,
            daily_limit_hits=0,
        )

        report = format_report(stats)
        assert "100" in report
        assert "80" in report
        assert "5" in report   # resumed
        assert "10" in report  # skipped
        assert "2" in report   # mismatches
        assert "3" in report   # failed

    def test_contains_header(self) -> None:
        """The report has a header with the tool name."""
        report = format_report(UploadStats())
        assert "gdrivecopy" in report

    def test_mismatch_details_shown(self) -> None:
        """Size mismatch details are listed in the report."""
        stats = UploadStats(
            size_mismatches=1,
            mismatch_details=["file.txt: local=100 bytes, drive=50 bytes"],
        )
        report = format_report(stats)
        assert "file.txt" in report
        assert "local=100" in report

    def test_errors_shown(self) -> None:
        """Errors are listed in the report."""
        stats = UploadStats(
            files_failed=1,
            errors=["big.bin: HTTP 500: Internal Server Error"],
        )
        report = format_report(stats)
        assert "big.bin" in report
        assert "500" in report

    def test_errors_truncated_after_20(self) -> None:
        """Only the first 20 errors are shown, with a '... and N more' note."""
        stats = UploadStats(
            files_failed=25,
            errors=[f"file_{i}.txt: error" for i in range(25)],
        )
        report = format_report(stats)
        assert "and 5 more" in report

    def test_empty_stats(self) -> None:
        """An empty UploadStats produces a valid report with zero counters."""
        report = format_report(UploadStats())
        assert "0" in report
        assert "gdrivecopy" in report


# ---------------------------------------------------------------------------
# save_report_json
# ---------------------------------------------------------------------------


class TestSaveReportJson:
    def test_creates_json_file(self, tmp_path: Path) -> None:
        """save_report_json writes a valid JSON file."""
        stats = UploadStats(files_scanned=10, files_uploaded=8)
        path = tmp_path / "report.json"

        save_report_json(stats, path)

        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["files_scanned"] == 10
        assert data["files_uploaded"] == 8

    def test_all_fields_present(self, tmp_path: Path) -> None:
        """The JSON file contains all expected fields."""
        stats = UploadStats(
            files_scanned=5,
            files_uploaded=3,
            bytes_uploaded=1024,
            files_resumed=1,
            files_skipped=1,
            size_mismatches=0,
            files_failed=0,
            duration_seconds=42.5,
            daily_limit_hits=0,
            errors=["e1"],
            mismatch_details=["m1"],
        )
        path = tmp_path / "report.json"
        save_report_json(stats, path)

        data = json.loads(path.read_text(encoding="utf-8"))
        expected_keys = {
            "files_scanned",
            "symlinks_skipped",
            "files_uploaded",
            "bytes_uploaded",
            "files_resumed",
            "files_skipped",
            "size_mismatches",
            "files_failed",
            "duration_seconds",
            "daily_limit_hits",
            "errors",
            "mismatch_details",
        }
        assert set(data.keys()) == expected_keys

    def test_duration_is_rounded(self, tmp_path: Path) -> None:
        """duration_seconds is rounded to 2 decimal places in the JSON."""
        stats = UploadStats(duration_seconds=1.23456789)
        path = tmp_path / "report.json"
        save_report_json(stats, path)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["duration_seconds"] == 1.23

    def test_errors_are_lists(self, tmp_path: Path) -> None:
        """errors and mismatch_details are serialized as JSON arrays."""
        stats = UploadStats(
            errors=["err1", "err2"],
            mismatch_details=["m1"],
        )
        path = tmp_path / "report.json"
        save_report_json(stats, path)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data["errors"], list)
        assert len(data["errors"]) == 2
        assert isinstance(data["mismatch_details"], list)

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        """Calling save_report_json twice overwrites the previous file."""
        path = tmp_path / "report.json"

        save_report_json(UploadStats(files_scanned=1), path)
        save_report_json(UploadStats(files_scanned=99), path)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["files_scanned"] == 99
