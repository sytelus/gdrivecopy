"""Tests for gdrivecopy.cli argument validation and command orchestration."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gdrivecopy import __version__
from gdrivecopy.cli import _build_parser, _parse_size, main
from gdrivecopy.models import UploadStats


class TestParseSize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("1", 1), ("256K", 256 * 1024), ("1.5M", 1_572_864), ("2g", 2 * 1024**3)],
    )
    def test_valid_sizes(self, value: str, expected: int) -> None:
        assert _parse_size(value) == expected

    @pytest.mark.parametrize("value", ["", "garbage", "0", "-1M", "nan", "inf"])
    def test_invalid_sizes(self, value: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_size(value)


class TestParser:
    @pytest.mark.parametrize(
        "arguments",
        [
            ["upload", ".", "   "],
            ["upload", ".", "folder", "--transfers", "0"],
            ["upload", ".", "folder", "--bwlimit", "0"],
            ["upload", ".", "folder", "--chunk-size", "255K"],
        ],
    )
    def test_invalid_numeric_options_exit_cleanly(self, arguments: list[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _build_parser().parse_args(arguments)
            # Chunk alignment is validated by main rather than argparse.
            main(arguments)
        assert exc_info.value.code == 2

    def test_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0
        assert __version__ in capsys.readouterr().out

    def test_missing_command_returns_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 1
        assert "usage:" in capsys.readouterr().out


class TestUploadCommand:
    @patch("gdrivecopy.cli._setup_logging")
    @patch("gdrivecopy.report.save_report_json")
    @patch("gdrivecopy.uploader.Uploader")
    @patch("gdrivecopy.drive.DriveClient")
    @patch("gdrivecopy.auth.authenticate")
    def test_builds_validated_config_and_writes_report(
        self,
        mock_authenticate: MagicMock,
        mock_drive_cls: MagicMock,
        mock_uploader_cls: MagicMock,
        mock_save_report: MagicMock,
        _mock_logging: MagicMock,
        tmp_path: Path,
    ) -> None:
        stats = UploadStats(files_uploaded=1, bytes_uploaded=123)
        mock_uploader_cls.return_value.run.return_value = stats
        log_dir = tmp_path / "logs"
        expected_log = (log_dir / "gdrivecopy_test.log").resolve()
        _mock_logging.return_value = expected_log

        main(
            [
                "upload",
                str(tmp_path),
                "folder-id",
                "--transfers",
                "2",
                "--chunk-size",
                "512K",
                "--bwlimit",
                "1M",
                "--log-dir",
                str(log_dir),
            ]
        )

        config = mock_uploader_cls.call_args.args[0]
        assert config.source_dir == tmp_path.resolve()
        assert config.transfers == 2
        assert config.chunk_size == 512 * 1024
        assert config.bwlimit == 1024**2
        assert config.log_path == expected_log
        mock_drive_cls.assert_called_once_with(mock_authenticate.return_value)
        mock_save_report.assert_called_once_with(stats, log_dir / "report.json")

    @pytest.mark.parametrize(
        "stats",
        [
            UploadStats(files_failed=1),
            UploadStats(scan_errors=1),
            UploadStats(size_mismatches=1),
            UploadStats(path_conflicts=1),
            UploadStats(quota_limit_hits=1),
        ],
    )
    @patch("gdrivecopy.cli._setup_logging")
    @patch("gdrivecopy.report.save_report_json")
    @patch("gdrivecopy.drive.DriveClient")
    @patch("gdrivecopy.auth.authenticate")
    def test_incomplete_upload_exits_nonzero(
        self,
        _mock_authenticate: MagicMock,
        _mock_drive_cls: MagicMock,
        _mock_save_report: MagicMock,
        _mock_logging: MagicMock,
        stats: UploadStats,
        tmp_path: Path,
    ) -> None:
        with patch("gdrivecopy.uploader.Uploader") as uploader_cls:
            uploader_cls.return_value.run.return_value = stats
            with pytest.raises(SystemExit) as exc_info:
                main(["upload", str(tmp_path), "folder-id"])

        assert exc_info.value.code == 1

    def test_missing_source_fails_before_authentication(self, tmp_path: Path) -> None:
        with (
            patch("gdrivecopy.auth.authenticate") as mock_authenticate,
            pytest.raises(SystemExit) as exc_info,
        ):
            main(["upload", str(tmp_path / "missing"), "folder-id"])

        assert exc_info.value.code == 2
        mock_authenticate.assert_not_called()

    @patch("gdrivecopy.cli._setup_logging")
    @patch("gdrivecopy.auth.authenticate")
    def test_expected_drive_error_exits_without_traceback(
        self,
        mock_authenticate: MagicMock,
        _mock_logging: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from gdrivecopy.drive import DriveApiError

        mock_authenticate.side_effect = DriveApiError(403, "denied")

        with pytest.raises(SystemExit) as exc_info:
            main(["upload", str(tmp_path), "folder-id", "--quiet"])

        assert exc_info.value.code == 1
        assert "upload aborted: denied" in capsys.readouterr().err


class TestAuthCommand:
    def test_overlapping_state_paths_fail_before_auth(self, tmp_path):
        path = str(tmp_path / "same.json")
        with (
            patch("gdrivecopy.auth.authenticate") as authenticate,
            pytest.raises(SystemExit) as exc,
        ):
            main(["auth", "--credentials", path, "--token", path])
        assert exc.value.code == 2
        authenticate.assert_not_called()

    def test_log_write_failure_exits_cleanly(self, capsys):
        with (
            patch("gdrivecopy.cli._setup_logging", side_effect=PermissionError("read-only")),
            pytest.raises(SystemExit) as exc,
        ):
            main(["auth"])
        assert exc.value.code == 1
        assert "filesystem operation failed" in capsys.readouterr().err

    @patch("gdrivecopy.cli._setup_logging")
    @patch("gdrivecopy.auth.authenticate")
    def test_auth_failure_exits_nonzero(
        self, mock_authenticate: MagicMock, _mock_logging: MagicMock
    ) -> None:
        mock_authenticate.side_effect = FileNotFoundError("missing credentials")

        with pytest.raises(SystemExit) as exc_info:
            main(["auth"])

        assert exc_info.value.code == 1
