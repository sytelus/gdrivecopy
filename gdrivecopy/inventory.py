"""Checkpointed Drive inventory refreshed through the account change feed."""

from __future__ import annotations

import json
from collections.abc import MutableMapping

from gdrivecopy.control import RunControl
from gdrivecopy.drive import FOLDER_MIME, DriveApiError, DrivePathConflictError
from gdrivecopy.jobstore import JobStore


def validate_item(item: dict) -> None:
    if not isinstance(item, dict):
        raise DriveApiError(502, "Invalid Drive metadata item")
    for field in ("id", "name", "mimeType"):
        if not isinstance(item.get(field), str) or not item[field]:
            raise DriveApiError(502, f"Invalid Drive {field}")
    if "/" in item["name"] or item["name"] in {".", ".."}:
        raise DrivePathConflictError(409, f"Ambiguous Drive name: {item['name']!r}")
    if "size" in item and (isinstance(item["size"], bool) or not str(item["size"]).isdigit()):
        raise DriveApiError(502, "Invalid Drive byte size")


class DriveInventory:
    def __init__(self, store: JobStore, drive, control: RunControl) -> None:
        self.store, self.drive, self.control = store, drive, control

    def sync(self, root_id: str) -> str:
        self.control.emit("phase", phase="Refreshing Drive inventory")
        root = self.drive.file_metadata(root_id)
        if root.get("mimeType") != FOLDER_MIME or root.get("trashed") or root.get("driveId"):
            raise DrivePathConflictError(
                409, "Choose an existing My Drive folder (shared drives are not supported)"
            )
        actual_id = root["id"]
        previous = self.store.get("root_id")
        if previous is not None and previous != actual_id:
            raise ValueError("Drive root changed; use the original account and destination")
        if previous is None:
            self._initialize(root)
        self._scan_pending()
        try:
            self._changes()
        except DriveApiError as exc:
            if exc.status != 410:
                raise
            self.control.emit("phase", phase="Change cursor expired; rebuilding Drive inventory")
            self._initialize(root)
        self._scan_pending()
        return actual_id

    def _initialize(self, root: dict) -> None:
        # Capture BEFORE scanning. Changes during a days-long inventory will be
        # reconciled afterward, instead of being lost in a snapshot/cache gap.
        token = self.drive.change_token()
        with self.store.transaction() as db:
            db.execute("DELETE FROM remote")
            db.execute("DELETE FROM folders")
            db.execute("DELETE FROM folder_seen")
            db.execute(
                "INSERT INTO remote VALUES(?,?,?,?,?)", ("", root["id"], None, 1, json.dumps(root))
            )
            db.execute("INSERT INTO folders(id,path) VALUES(?, '')", (root["id"],))
            for key, value in (("root_id", root["id"]), ("change_cursor", token)):
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    @staticmethod
    def _drop_subtree(db, path: str) -> None:
        prefix = path + "/"
        # '/' sorts immediately before '0': use indexed prefix ranges rather
        # than scanning the whole inventory once per removed item.
        upper = path + "0"
        db.execute(
            "DELETE FROM folders WHERE path=? OR (path>=? AND path<?)", (path, prefix, upper)
        )
        db.execute("DELETE FROM remote WHERE path=? OR (path>=? AND path<?)", (path, prefix, upper))

    def _scan_pending(self) -> None:
        restarted = False
        current_id = None
        while True:
            self.control.check()
            with self.store._lock:
                row = self.store.db.execute(
                    "SELECT * FROM folders WHERE status='pending' ORDER BY path LIMIT 1"
                ).fetchone()
            if row is None:
                return
            folder = dict(row)
            if current_id != folder["id"]:
                current_id, restarted = folder["id"], False
            if folder["token"] is None:
                with self.store.transaction() as db:
                    db.execute("DELETE FROM folder_seen WHERE parent=?", (folder["id"],))
                    db.execute("DELETE FROM page_tokens WHERE parent=?", (folder["id"],))
            try:
                page = self.drive.folder_page(folder["id"], folder["token"])
            except DriveApiError as exc:
                if exc.status != 400 or not folder["token"] or restarted:
                    raise
                # Listing cursors can expire over a long interruption. Restart
                # only this folder; already scanned subtrees remain on disk.
                with self.store.transaction() as db:
                    db.execute("UPDATE folders SET token=NULL WHERE id=?", (folder["id"],))
                restarted = True
                self.control.emit(
                    "retry",
                    folder["path"],
                    message="Expired listing cursor; rescanning this folder",
                )
                continue
            token = page.get("nextPageToken")
            if token is not None and (not isinstance(token, str) or token == folder["token"]):
                raise DriveApiError(502, "Invalid/repeated Drive page token")
            with self.store.transaction() as db:
                if token:
                    if db.execute(
                        "SELECT 1 FROM page_tokens WHERE parent=? AND token=?",
                        (folder["id"], token),
                    ).fetchone():
                        raise DriveApiError(502, "Drive repeated an earlier listing cursor")
                    db.execute("INSERT INTO page_tokens VALUES(?,?)", (folder["id"], token))
                for item in page.get("files", []):
                    validate_item(item)
                    path = f"{folder['path']}/{item['name']}".lstrip("/")
                    previous = db.execute("SELECT * FROM remote WHERE path=?", (path,)).fetchone()
                    if previous is not None and previous["id"] != item["id"]:
                        seen = db.execute(
                            "SELECT 1 FROM folder_seen WHERE parent=? AND id=?",
                            (folder["id"], previous["id"]),
                        ).fetchone()
                        if seen:
                            raise DrivePathConflictError(409, f"Duplicate Drive path: {path}")
                        self._drop_subtree(db, path)
                    old_id = db.execute(
                        "SELECT path FROM remote WHERE id=?", (item["id"],)
                    ).fetchone()
                    if old_id is not None and (
                        old_id["path"] == ""
                        or folder["path"] == old_id["path"]
                        or folder["path"].startswith(old_id["path"] + "/")
                    ):
                        raise DrivePathConflictError(409, "Drive listing contains a folder cycle")
                    if old_id is not None and old_id["path"] != path:
                        self._drop_subtree(db, old_id["path"])
                    is_folder = item["mimeType"] == FOLDER_MIME
                    db.execute(
                        "INSERT OR REPLACE INTO remote VALUES(?,?,?,?,?)",
                        (path, item["id"], folder["id"], is_folder, json.dumps(item)),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO folder_seen VALUES(?,?)", (folder["id"], item["id"])
                    )
                    if is_folder:
                        db.execute(
                            "INSERT OR IGNORE INTO folders(id,path) VALUES(?,?)", (item["id"], path)
                        )
                if token:
                    db.execute("UPDATE folders SET token=? WHERE id=?", (token, folder["id"]))
                else:
                    after = ""
                    while missing := db.execute(
                        "SELECT path FROM remote WHERE parent=? AND path>? AND id NOT IN "
                        "(SELECT id FROM folder_seen WHERE parent=?) ORDER BY path LIMIT 500",
                        (folder["id"], after, folder["id"]),
                    ).fetchall():
                        self.control.check()
                        for absent in missing:
                            self._drop_subtree(db, absent["path"])
                        after = missing[-1]["path"]
                    db.execute(
                        "UPDATE folders SET status='done',token=NULL WHERE id=?", (folder["id"],)
                    )
                    db.execute("DELETE FROM folder_seen WHERE parent=?", (folder["id"],))
                    db.execute("DELETE FROM page_tokens WHERE parent=?", (folder["id"],))
            self.control.emit("scan", count=len(page.get("files", [])))

    def _changes(self) -> None:
        token = self.store.get("change_cursor")
        while True:
            self.control.check()
            page = self.drive.change_page(token)
            next_token = page.get("nextPageToken") or page.get("newStartPageToken")
            if not isinstance(next_token, str) or not next_token:
                raise DriveApiError(502, "Drive omitted its next change cursor")
            if page.get("nextPageToken") and next_token == token:
                raise DriveApiError(502, "Drive repeated a change cursor")
            with self.store.transaction() as db:
                for change in page.get("changes", []):
                    known = db.execute(
                        "SELECT parent FROM remote WHERE id=?", (change.get("fileId"),)
                    ).fetchone()
                    parents = set(change.get("file", {}).get("parents", []))
                    if known is not None:
                        parents.add(known["parent"])
                    for parent in parents:
                        # Rescan only affected known parents. New/moved subtrees
                        # are discovered by their parent's listing, not guessed
                        # from potentially out-of-order change records.
                        db.execute(
                            "UPDATE folders SET status='pending',token=NULL WHERE id=?", (parent,)
                        )
                db.execute(
                    "INSERT OR REPLACE INTO meta VALUES('change_cursor',?)",
                    (json.dumps(next_token),),
                )
            if not page.get("nextPageToken"):
                return
            if next_token == token:
                raise DriveApiError(502, "Drive repeated a change cursor")
            token = next_token

    def at(self, path: str) -> dict | None:
        with self.store._lock:
            row = self.store.db.execute("SELECT * FROM remote WHERE path=?", (path,)).fetchone()
        return dict(row) if row else None


class FolderMap(MutableMapping):
    """Legacy uploader folder-map interface backed by indexed SQLite lookups."""

    def __init__(self, store: JobStore) -> None:
        self.store = store

    def __getitem__(self, key):
        path = key.rstrip("/")
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT id FROM remote WHERE path=? AND folder=1", (path,)
            ).fetchone()
        if row is None:
            raise KeyError(key)
        return row["id"]

    def __setitem__(self, key, value):
        path = key.rstrip("/")
        parent_path = path.rpartition("/")[0]
        parent = self[parent_path]
        data = {
            "id": value,
            "name": path.rpartition("/")[2],
            "mimeType": FOLDER_MIME,
            "parents": [parent],
        }
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO remote VALUES(?,?,?,?,?)",
                (path, value, parent, 1, json.dumps(data)),
            )
            db.execute(
                "INSERT OR REPLACE INTO folders(id,path,status) VALUES(?,?,'done')", (value, path)
            )

    def __delitem__(self, key):
        raise TypeError("Folder deletion is not supported")

    def __iter__(self):
        for row in self.store.rows("SELECT path FROM remote WHERE folder=1"):
            yield row["path"] + ("/" if row["path"] else "")

    def __len__(self):
        with self.store._lock:
            return self.store.db.execute("SELECT COUNT(*) FROM remote WHERE folder=1").fetchone()[0]
