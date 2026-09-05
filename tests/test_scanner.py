"""Tests for gdrivecopy.scanner -- local filesystem scanner."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gdrivecopy.scanner import _creation_time, _is_link_like, _iso_from_timestamp, scan_local

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


class TestCreationTime:
    def test_linux_change_time_is_not_used_as_creation_time(self) -> None:
        """POSIX ctime is metadata-change time and must not be uploaded as birth time."""
        stat_result = SimpleNamespace(st_ctime=123.0)
        with patch("gdrivecopy.scanner.os.name", "posix"):
            assert _creation_time(stat_result) is None  # type: ignore[arg-type]

    def test_birth_time_is_used_when_available(self) -> None:
        """Platforms exposing a birth timestamp preserve it."""
        stat_result = SimpleNamespace(st_ctime=123.0, st_birthtime=0.0)
        with patch("gdrivecopy.scanner.os.name", "posix"):
            assert _creation_time(stat_result) == "1970-01-01T00:00:00+00:00"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# scan_local -- regular files
# ---------------------------------------------------------------------------


class TestScanLocalRegularFiles:
    def test_finds_all_regular_files(self, source_tree: Path) -> None:
        """scan_local returns one LocalFile per regular file in the tree."""
        result = scan_local(source_tree)
        assert len(result.files) == 3

    def test_relative_paths_use_forward_slash(self, source_tree: Path) -> None:
        """Relative paths always use '/' regardless of OS."""
        files = scan_local(source_tree).files
        rel_paths = {f.relative_path for f in files}
        assert "file_a.txt" in rel_paths
        assert "subdir/file_b.bin" in rel_paths
        assert "subdir/nested/file_c.dat" in rel_paths

    def test_file_sizes_are_correct(self, source_tree: Path) -> None:
        """Each LocalFile records the correct byte size."""
        by_path = {f.relative_path: f for f in scan_local(source_tree).files}
        assert by_path["file_a.txt"].size == 11
        assert by_path["subdir/file_b.bin"].size == 5
        assert by_path["subdir/nested/file_c.dat"].size == 3

    def test_paths_are_absolute(self, source_tree: Path) -> None:
        """The `path` attribute is an absolute path to the real file."""
        for f in scan_local(source_tree).files:
            assert f.path.is_absolute()
            assert f.path.exists()

    def test_mtime_is_set(self, source_tree: Path) -> None:
        """mtime is a non-empty ISO 8601 string."""
        for f in scan_local(source_tree).files:
            assert f.mtime
            assert "T" in f.mtime

    def test_ctime_is_real_or_absent(self, source_tree: Path) -> None:
        """Creation time is present only where the platform exposes one."""
        for f in scan_local(source_tree).files:
            if os.name == "nt" or hasattr(f.path.stat(), "st_birthtime"):
                assert f.ctime is not None
                assert "T" in f.ctime
            else:
                assert f.ctime is None

    def test_exact_excluded_files_are_not_scanned(self, source_tree: Path) -> None:
        """Tool-owned files can be omitted without excluding neighboring data."""
        excluded = source_tree / "file_a.txt"

        result = scan_local(source_tree, {excluded})

        assert result.files_excluded == 1
        assert {item.relative_path for item in result.files} == {
            "subdir/file_b.bin",
            "subdir/nested/file_c.dat",
        }


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

        names = [f.relative_path for f in scan_local(root).files]
        assert names == ["a.txt", "m.txt", "z.txt"]

    def test_directories_are_traversed_in_sorted_order(self, tmp_path: Path) -> None:
        """Subdirectories are visited in alphabetical order."""
        root = tmp_path / "dirs"
        root.mkdir()
        for d in ["z_dir", "a_dir", "m_dir"]:
            sub = root / d
            sub.mkdir()
            (sub / "file.txt").write_text("x")

        dirs = [f.relative_path.split("/")[0] for f in scan_local(root).files]
        assert dirs == ["a_dir", "m_dir", "z_dir"]


# ---------------------------------------------------------------------------
# scan_local -- symlinks
# ---------------------------------------------------------------------------


class TestScanLocalSymlinks:
    def test_junctions_are_link_like(self) -> None:
        """Windows junction support remains optional on older Python versions."""
        path = SimpleNamespace(is_symlink=lambda: False, is_junction=lambda: True)
        assert _is_link_like(path) is True  # type: ignore[arg-type]

    def test_old_python_junctions_are_link_like(self) -> None:
        """The reparse-tag fallback also protects Python 3.10/3.11."""
        path = SimpleNamespace(
            is_symlink=lambda: False,
            lstat=lambda: SimpleNamespace(st_file_attributes=0x400, st_reparse_tag=0xA0000003),
        )
        with patch("gdrivecopy.scanner.os.name", "nt"):
            assert _is_link_like(path) is True  # type: ignore[arg-type]

    def test_cloud_placeholders_are_not_links(self) -> None:
        path = SimpleNamespace(
            is_symlink=lambda: False,
            lstat=lambda: SimpleNamespace(st_file_attributes=0x400, st_reparse_tag=0x9000001A),
        )
        with patch("gdrivecopy.scanner.os.name", "nt"):
            assert _is_link_like(path) is False

    def test_private_temporary_files_are_excluded(self, source_tree: Path) -> None:
        token = source_tree / "token.json"
        (source_tree / ".token.json.random.tmp").write_text("secret")
        (source_tree / ".unrelated.random.tmp").write_text("payload")
        result = scan_local(source_tree, {token})
        assert result.files_excluded == 1
        assert ".unrelated.random.tmp" in {file.relative_path for file in result.files}

    def test_symlinks_are_skipped(self, source_tree_with_symlink: Path) -> None:
        """Symlinks must not appear in the scan output."""
        result = scan_local(source_tree_with_symlink)
        rel_paths = {f.relative_path for f in result.files}
        assert "link_to_a.txt" not in rel_paths
        assert result.symlinks_skipped == 1

    def test_symlink_logs_warning(
        self, source_tree_with_symlink: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning is logged for each skipped symlink."""
        import logging

        with caplog.at_level(logging.WARNING, logger="gdrivecopy.scanner"):
            scan_local(source_tree_with_symlink)
        assert any("Skipping symlink" in m for m in caplog.messages)

    def test_directory_symlinks_are_counted_and_not_traversed(self, tmp_path: Path) -> None:
        """Directory links are explicit skips, not invisible omissions."""
        root = tmp_path / "root"
        target = tmp_path / "target"
        root.mkdir()
        target.mkdir()
        (target / "outside.txt").write_text("outside")
        (root / "linked_dir").symlink_to(target, target_is_directory=True)

        result = scan_local(root)

        assert result.files == []
        assert result.symlinks_skipped == 1


# ---------------------------------------------------------------------------
# scan_local -- stat errors
# ---------------------------------------------------------------------------


class TestScanLocalStatErrors:
    def test_directory_walk_failure_is_reported(self, source_tree):
        def failed_walk(root, *, followlinks, onerror):
            onerror(PermissionError(13, "denied", str(root / "private")))
            return iter(())

        with patch("gdrivecopy.scanner.os.walk", side_effect=failed_walk):
            result = scan_local(source_tree)
        assert len(result.errors) == 1
        assert "private" in result.errors[0]

    def test_stat_error_skips_file(self, tmp_path: Path) -> None:
        """If stat() raises OSError the file is skipped without crashing."""
        root = tmp_path / "stat_err"
        root.mkdir()
        (root / "good.txt").write_text("ok")
        (root / "bad.txt").write_text("will fail")

        original_stat = Path.stat

        def _patched_stat(self_: Path, *args: object, **kwargs: object) -> os.stat_result:
            follow = kwargs.get("follow_symlinks", True)
            if self_.name == "bad.txt" and follow:
                raise OSError("permission denied")
            return original_stat(self_, *args, **kwargs)

        with patch.object(Path, "stat", _patched_stat):
            result = scan_local(root)

        names = [f.relative_path for f in result.files]
        assert "good.txt" in names
        assert "bad.txt" not in names
        assert len(result.errors) == 1

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
            scan_local(root)

        assert any("Cannot inspect" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# scan_local -- empty directory
# ---------------------------------------------------------------------------


class TestScanLocalEmpty:
    def test_empty_directory_yields_nothing(self, tmp_path: Path) -> None:
        """An empty source directory produces zero results."""
        root = tmp_path / "empty"
        root.mkdir()
        result = scan_local(root)
        assert result.files == []
        assert result.symlinks_skipped == 0
        assert result.errors == []

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        """Direct library callers get a clear error for an invalid source root."""
        with pytest.raises(NotADirectoryError, match="does not exist"):
            scan_local(tmp_path / "missing")
