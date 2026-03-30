"""Tests for gdrivecopy.scanner -- local filesystem scanner."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from gdrivecopy.models import LocalFile
from gdrivecopy.scanner import _iso_from_timestamp, scan_local


# ---------------------------------------------------------------------------
# _iso_from_timestamp
# ---------------------------------------------------------------------------


class TestIsoFromTimestamp:
    def test_returns_utc_iso_string(self) -> None:
        """Verify that a POSIX timestamp is converted to ISO 8601 UTC."""
        result = _iso_from_timestamp(0.0)
        assert result == "1970-01-01T00:00:00+00:00"

    def test_fractional_seconds(self) -> None:
        """Verify fractional timestamps are handled."""
        result = _iso_from_timestamp(1_700_000_000.5)
        assert "+00:00" in result


# ---------------------------------------------------------------------------
# scan_local -- regular files
# ---------------------------------------------------------------------------


class TestScanLocalRegularFiles:
    def test_finds_all_regular_files(self, source_tree: Path) -> None:
        """scan_local yields one LocalFile per regular file in the tree."""
        files = list(scan_local(source_tree))
        assert len(files) == 3

    def test_relative_paths_use_forward_slash(self, source_tree: Path) -> None:
        """Relative paths always use '/' regardless of OS."""
        files = list(scan_local(source_tree))
        rel_paths = {f.relative_path for f in files}
        assert "file_a.txt" in rel_paths
        assert "subdir/file_b.bin" in rel_paths
        assert "subdir/nested/file_c.dat" in rel_paths

    def test_file_sizes_are_correct(self, source_tree: Path) -> None:
        """Each LocalFile records the correct byte size."""
        files = {f.relative_path: f for f in scan_local(source_tree)}
        assert files["file_a.txt"].size == 11
        assert files["subdir/file_b.bin"].size == 5
        assert files["subdir/nested/file_c.dat"].size == 3

    def test_paths_are_absolute(self, source_tree: Path) -> None:
        """The `path` attribute is an absolute path to the real file."""
        files = list(scan_local(source_tree))
        for f in files:
            assert f.path.is_absolute()
            assert f.path.exists()

    def test_mtime_is_set(self, source_tree: Path) -> None:
        """mtime is a non-empty ISO 8601 string."""
        files = list(scan_local(source_tree))
        for f in files:
            assert f.mtime
            assert "T" in f.mtime  # basic ISO check

    def test_ctime_is_set(self, source_tree: Path) -> None:
        """ctime is a non-empty ISO 8601 string."""
        files = list(scan_local(source_tree))
        for f in files:
            assert f.ctime is not None
            assert "T" in f.ctime


# ---------------------------------------------------------------------------
# scan_local -- sort order
# ---------------------------------------------------------------------------


class TestScanLocalSortOrder:
    def test_files_are_sorted_within_directory(self, tmp_path: Path) -> None:
        """Files within a single directory appear in sorted order."""
        root = tmp_path / "sorted"
        root.mkdir()
        for name in ["z.txt", "a.txt", "m.txt"]:
            (root / name).write_text("x")

        files = list(scan_local(root))
        names = [f.relative_path for f in files]
        assert names == ["a.txt", "m.txt", "z.txt"]

    def test_directories_are_traversed_in_sorted_order(self, tmp_path: Path) -> None:
        """Subdirectories are visited in alphabetical order."""
        root = tmp_path / "dirs"
        root.mkdir()
        for d in ["z_dir", "a_dir", "m_dir"]:
            sub = root / d
            sub.mkdir()
            (sub / "file.txt").write_text("x")

        files = list(scan_local(root))
        dirs = [f.relative_path.split("/")[0] for f in files]
        assert dirs == ["a_dir", "m_dir", "z_dir"]


# ---------------------------------------------------------------------------
# scan_local -- symlinks
# ---------------------------------------------------------------------------


class TestScanLocalSymlinks:
    def test_symlinks_are_skipped(self, source_tree_with_symlink: Path) -> None:
        """Symlinks must not appear in the scan output."""
        files = list(scan_local(source_tree_with_symlink))
        rel_paths = {f.relative_path for f in files}
        assert "link_to_a.txt" not in rel_paths

    def test_symlink_logs_warning(
        self, source_tree_with_symlink: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning is logged for each skipped symlink."""
        import logging

        with caplog.at_level(logging.WARNING, logger="gdrivecopy.scanner"):
            list(scan_local(source_tree_with_symlink))
        assert any("Skipping symlink" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# scan_local -- stat errors
# ---------------------------------------------------------------------------


class TestScanLocalStatErrors:
    def test_stat_error_skips_file(self, tmp_path: Path) -> None:
        """If stat() raises OSError the file is skipped without crashing."""
        root = tmp_path / "stat_err"
        root.mkdir()
        (root / "good.txt").write_text("ok")
        (root / "bad.txt").write_text("will fail")

        original_stat = Path.stat

        def _patched_stat(self_: Path, *args: object, **kwargs: object) -> os.stat_result:
            # Only fail on the explicit stat() call (follow_symlinks=True, the
            # default).  is_symlink() calls stat(follow_symlinks=False) which
            # must be allowed through so the scanner can reach the stat() path.
            follow = kwargs.get("follow_symlinks", True)
            if self_.name == "bad.txt" and follow:
                raise OSError("permission denied")
            return original_stat(self_, *args, **kwargs)

        with patch.object(Path, "stat", _patched_stat):
            files = list(scan_local(root))

        names = [f.relative_path for f in files]
        assert "good.txt" in names
        assert "bad.txt" not in names

    def test_stat_error_logs_message(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An error-level log message is emitted for stat failures."""
        import logging

        root = tmp_path / "stat_log"
        root.mkdir()
        (root / "bad.txt").write_text("will fail")

        original_stat = Path.stat

        def _patched_stat(self_: Path, *args: object, **kwargs: object) -> os.stat_result:
            follow = kwargs.get("follow_symlinks", True)
            if self_.name == "bad.txt" and follow:
                raise OSError("permission denied")
            return original_stat(self_, *args, **kwargs)

        with (
            patch.object(Path, "stat", _patched_stat),
            caplog.at_level(logging.ERROR, logger="gdrivecopy.scanner"),
        ):
            list(scan_local(root))

        assert any("Cannot stat" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# scan_local -- empty directory
# ---------------------------------------------------------------------------


class TestScanLocalEmpty:
    def test_empty_directory_yields_nothing(self, tmp_path: Path) -> None:
        """An empty source directory produces zero results."""
        root = tmp_path / "empty"
        root.mkdir()
        assert list(scan_local(root)) == []
