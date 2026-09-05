"""Failure-boundary tests for local state writes."""

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from gdrivecopy.persistence import write_text_atomic


def test_failed_replace_preserves_original_and_removes_temporary(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("original")
    with (
        patch.object(Path, "replace", side_effect=PermissionError("locked")),
        pytest.raises(PermissionError),
    ):
        write_text_atomic(path, "new")
    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]


def test_fixed_temporary_symlink_is_never_followed(tmp_path):
    target = tmp_path / "source.txt"
    target.write_text("source data")
    path = tmp_path / "token.json"
    path.with_name("token.json.tmp").symlink_to(target)
    write_text_atomic(path, "private token")
    assert target.read_text() == "source data"
    assert path.read_text() == "private token"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows relies on directory ACLs")
def test_new_state_is_private(tmp_path):
    path = tmp_path / "state.json"
    write_text_atomic(path, "private")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
