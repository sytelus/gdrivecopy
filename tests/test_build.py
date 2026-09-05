"""Release provenance must describe the packaged source, not a nearby checkout."""

from unittest.mock import patch

import pytest

from scripts.build_binary import ROOT, source_revision


@pytest.mark.parametrize("status,dirty", [("", False), (" M gdrivecopy/cli.py", True)])
def test_build_records_commit_and_local_modifications(status, dirty):
    with patch(
        "scripts.build_binary.subprocess.check_output", side_effect=[str(ROOT), "abc123", status]
    ):
        assert source_revision() == {"commit": "abc123", "dirty": dirty}


def test_source_archive_does_not_inherit_parent_repository_commit():
    with patch(
        "scripts.build_binary.subprocess.check_output", return_value=str(ROOT.parent)
    ) as git:
        assert source_revision() == {"commit": None, "dirty": None}
    assert git.call_count == 1


def test_build_without_git_reports_unknown_revision():
    with patch("scripts.build_binary.subprocess.check_output", side_effect=FileNotFoundError):
        assert source_revision() == {"commit": None, "dirty": None}
