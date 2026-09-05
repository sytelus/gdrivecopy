"""Incremental inventory must not lose, duplicate, or misplace cached paths."""

from unittest.mock import patch

import pytest

from gdrivecopy.control import RunControl
from gdrivecopy.drive import DriveApiError, DrivePathConflictError
from gdrivecopy.inventory import DriveInventory
from gdrivecopy.jobstore import JobStore
from tests.fake_drive import FakeDrive


@pytest.fixture
def inventory(tmp_path):
    store = JobStore(tmp_path)
    drive = FakeDrive()
    control = RunControl()
    drive.control = control
    yield DriveInventory(store, drive, control), drive
    store.close()


def test_removed_folder_drops_descendants_without_matching_sibling_prefix(inventory):
    inv, drive = inventory
    folder = drive.create_folder("a", "root")
    sibling = drive.create_folder("a-other", "root")
    drive.add_file("file", b"data", folder)
    drive.add_file("keep", b"data", sibling)
    inv.sync("root")
    drive.trash_file(folder)
    inv.sync("root")
    assert inv.at("a") is None and inv.at("a/file") is None
    assert inv.at("a-other/keep")


def test_renamed_folder_rebuilds_its_descendant_paths(inventory):
    inv, drive = inventory
    folder = drive.create_folder("old", "root")
    drive.add_file("file", b"data", folder)
    inv.sync("root")
    drive.items[folder]["name"] = "new"
    drive.changes.append({"fileId": folder, "file": dict(drive.items[folder])})
    inv.sync("root")
    assert inv.at("old/file") is None
    assert inv.at("new/file")


def test_cycle_is_rejected_without_dropping_root(inventory):
    inv, drive = inventory
    drive.items["root"]["parents"] = ["root"]
    with pytest.raises(DrivePathConflictError, match="cycle"):
        inv.sync("root")
    assert inv.at("")


def test_duplicate_path_across_pages_rolls_back_ambiguous_page(inventory):
    inv, drive = inventory
    first = drive.add_file("duplicate", b"one")
    second = drive.add_file("duplicate", b"two")
    with (
        patch.object(
            drive,
            "folder_page",
            side_effect=[
                {"files": [drive.items[first]], "nextPageToken": "next"},
                {"files": [drive.items[second]]},
            ],
        ),
        pytest.raises(DrivePathConflictError, match="Duplicate"),
    ):
        inv.sync("root")
    assert inv.at("duplicate")["id"] == first


def test_nonadjacent_repeated_listing_token_fails_closed(inventory):
    inv, drive = inventory
    with (
        patch.object(
            drive,
            "folder_page",
            side_effect=[
                {"files": [], "nextPageToken": "a"},
                {"files": [], "nextPageToken": "b"},
                {"files": [], "nextPageToken": "a"},
            ],
        ),
        pytest.raises(DriveApiError, match="earlier listing cursor"),
    ):
        inv.sync("root")


def test_expired_change_cursor_rebuilds_inventory(inventory):
    inv, drive = inventory
    drive.add_file("file", b"data")
    inv.sync("root")
    calls = drive.page_calls
    with patch.object(drive, "change_page", side_effect=DriveApiError(410, "expired")):
        inv.sync("root")
    assert drive.page_calls == calls + 1
    assert inv.at("file")
