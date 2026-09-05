"""Durable bidirectional copy jobs built on the tested upload protocol engine."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from itertools import islice
from pathlib import Path

from gdrivecopy.control import Cancelled, ProgressModel, RunControl
from gdrivecopy.downloader import Downloader, safe_target
from gdrivecopy.drive import DriveApiError, QuotaLimitError
from gdrivecopy.inventory import DriveInventory, FolderMap
from gdrivecopy.jobstore import DatabaseSessions, JobStore, utc_now
from gdrivecopy.models import LocalFile, UploadConfig
from gdrivecopy.persistence import atomic_text_writer, write_text_atomic
from gdrivecopy.redaction import safe_error
from gdrivecopy.scanner import scan_local
from gdrivecopy.uploader import Uploader, _BandwidthLimiter

logger = logging.getLogger(__name__)
TERMINAL = {
    "copied",
    "copied_unverified",
    "exported",
    "skipped_size",
    "skipped_verified",
    "conflict",
    "unsupported",
}
EXPORT_TYPES = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/svg+xml", ".svg"),
}


def selected(path: str, config: dict) -> bool:
    patterns = config.get("patterns") or ["*"]
    return any(
        fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(path.rpartition("/")[2], p)
        for p in patterns
    ) and not any(fnmatch.fnmatchcase(path, p) for p in config.get("exclude", []))


class PersistentFolderIds:
    def __init__(self, store: JobStore):
        self.store = store

    @staticmethod
    def key(key):
        return "folder_id:" + json.dumps(key)

    def __contains__(self, key):
        return self.store.get(self.key(key)) is not None

    def __getitem__(self, key):
        return self.store.get(self.key(key))

    def __setitem__(self, key, value):
        self.store.set(self.key(key), value)


class TransferRunner:
    def __init__(
        self, store: JobStore, drive, config: dict, model: ProgressModel | None = None
    ) -> None:
        self.store, self.drive, self.config = store, drive, config
        self.model = model or ProgressModel()
        self.control = RunControl(on_event=self._event)
        self.inventory = DriveInventory(store, drive, self.control)
        self.drive.control = self.control
        self.drive._folder_ids = PersistentFolderIds(store)
        self._id_lock = threading.Lock()
        self._id_pool: list[str] = []
        self._bytes = 0
        self._resumed = 0

    def _event(self, kind: str, path: str = "", **data) -> None:
        def scrub(value):
            if isinstance(value, str):
                return safe_error(value)
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [scrub(item) for item in value]
            return value

        data = scrub(data)
        self.model.event(kind, path, **data)
        if kind not in {"progress", "scan", "plan"}:
            self.store.event(kind, path, **data)
            if kind in {"phase", "retry", "error", "conflict", "unsupported"}:
                logger.info("%s %s %s", kind, path, safe_error(data))

    def _reserve_id(self, lf: LocalFile) -> str:
        with self._id_lock:
            row = self.store.file(lf.relative_path)
            if row["remote_id"]:
                return row["remote_id"]
            if not self._id_pool:
                self._id_pool = self.drive.generate_ids(100)
            identity = self._id_pool.pop()
            self.store.update_file(lf.relative_path, remote_id=identity)
            return identity

    def _discard_id(self, lf: LocalFile) -> None:
        self.store.update_file(lf.relative_path, remote_id=None, session=None)

    def run(self) -> dict:
        start = time.monotonic()
        status = "failed"
        self.store.set("status", "running")
        self.store.set("last_started", utc_now())
        self.store.set("fatal_error", None)
        self.store.set("stop_reason", None)
        self.store.event(
            "run_started", direction=self.config["direction"], account=self.config["account_email"]
        )
        try:
            root_id = self.inventory.sync(self.config["remote_root"])
            self.store.reset_incomplete()
            self._plan()
            self._reconcile_completed()
            counts = self.store.counts()
            self.control.emit(
                "plan",
                files=sum(c["files"] for c in counts.values()),
                bytes=sum(c["bytes"] for c in counts.values()),
                finished_files=sum(c["files"] for s, c in counts.items() if s in TERMINAL),
                finished_bytes=sum(c["bytes"] for s, c in counts.items() if s in TERMINAL),
            )
            self.control.emit(
                "phase",
                phase="Checking copy plan" if self.config["dry_run"] else "Copying and verifying",
            )
            self._dispatch(root_id)
            status = "planned" if self.config["dry_run"] else "complete"
        except QuotaLimitError as exc:
            status = "paused"
            self.store.set("stop_reason", "Drive quota reached: " + safe_error(exc))
            self.store.event(status, message=safe_error(exc))
        except Cancelled as exc:
            status = "cancelled" if self.control.reason == "Cancelled by user" else "paused"
            self.store.set("stop_reason", self.control.reason)
            self.store.event(status, message=safe_error(exc))
        except Exception as exc:
            self.store.set("fatal_error", safe_error(exc))
            self.store.event("fatal_error", message=safe_error(exc))
            logger.error("Job stopped: %s", safe_error(exc))
        finally:
            counts = self.store.counts()
            if status in {"complete", "planned"} and (
                any(
                    counts.get(s, {}).get("files", 0)
                    for s in {"failed", "conflict", "unsupported", "running", "cancelled"}
                )
                or (status == "complete" and counts.get("pending", {}).get("files", 0))
                or self.store.get("scan_errors", 0)
            ):
                status = "incomplete"
            elapsed = time.monotonic() - start
            self.store.set("status", status)
            self.store.set("last_finished", utc_now())
            self.store.set("duration_seconds", elapsed)
            self._bytes = self.model.snapshot()["wire_bytes"]
            self.store.set("bytes_this_run", self._bytes)
            self.store.set("resumed_bytes_this_run", self._resumed)
            self.store.set("bytes_total_runs", self.store.get("bytes_total_runs", 0) + self._bytes)
            self.store.set("seconds_total_runs", self.store.get("seconds_total_runs", 0) + elapsed)
            self.store.event("run_finished", status=status, bytes=self._bytes, seconds=elapsed)
        report = build_report(self.store)
        write_text_atomic(self.store.directory / "report.json", json.dumps(report, indent=2))
        self._write_file_report()
        return report

    def _plan(self) -> None:
        if self.store.get("plan_complete"):
            self.control.emit("phase", phase="Reusing saved manifest")
            return
        self.control.emit("phase", phase="Building file manifest")
        # No payload is dispatched until plan_complete is durable. An interrupted
        # build must restart from one current scan, without retaining removed or
        # changed files (or namespace decisions) from its earlier partial scan.
        with self.store.transaction() as db:
            db.execute("DELETE FROM files")
            for table in ("targets", "target_nodes", "target_conflicts"):
                db.execute(f"DROP TABLE IF EXISTS {table}")
        buffer = []

        def add(path, data, size):
            if not selected(path, self.config):
                return
            buffer.append((path, data, size))
            if len(buffer) >= 500:
                self.store.add_files(buffer)
                buffer.clear()
            self.control.emit("scan")

        if self.config["direction"] == "upload":

            def on_file(lf):
                data = asdict(lf)
                data["path"] = str(lf.path)
                add(lf.relative_path, data, lf.size)

            scan = scan_local(
                Path(self.config["local_path"]),
                excluded_paths=[Path(p) for p in self.config.get("excluded_files", [])],
                excluded_directories=[Path(self.config["state_dir"])],
                on_file=on_file,
                check_cancel=self.control.check,
            )
            self.store.set("scan_errors", len(scan.errors))
            self.store.set("scan_errors_sample", [safe_error(error) for error in scan.errors[:20]])
            self.store.set("symlinks_skipped", scan.symlinks_skipped)
            self.store.set("tool_entries_excluded", scan.files_excluded)
            for error in scan.errors:
                self.store.event("scan_error", message=safe_error(error))
        else:
            for remote in self.store.rows(
                "SELECT path,data FROM remote WHERE folder=0 ORDER BY path"
            ):
                self.control.check()
                metadata = json.loads(remote["data"])
                data = {"remote": metadata, "target": remote["path"]}
                if self.config.get("export_docs") and metadata["mimeType"] in EXPORT_TYPES:
                    mime, suffix = EXPORT_TYPES[metadata["mimeType"]]
                    if self.config["export_docs"] == "pdf":
                        mime, suffix = "application/pdf", ".pdf"
                    data.update(target=remote["path"] + suffix, export_mime=mime)
                add(remote["path"], data, int(metadata.get("size") or 0))
        self.store.add_files(buffer)
        # Resolve the entire download namespace BEFORE writing any payload.
        if self.config["direction"] == "download":
            self._validate_download_targets()
        self.store.set("plan_complete", True)

    def _validate_download_targets(self) -> None:
        """Validate directory aliases too: A/x and a/y must not merge silently."""
        with self.store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS targets(name TEXT PRIMARY KEY, path TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS target_nodes(name TEXT PRIMARY KEY, spelling TEXT, kind TEXT)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS target_conflicts(name TEXT PRIMARY KEY)")
        iterator = self.store.rows("SELECT path,data FROM files ORDER BY path")
        while batch := list(islice(iterator, 500)):
            self.control.check()
            with self.store.transaction() as db:
                for row in batch:
                    data = json.loads(row["data"])
                    try:
                        safe_target(Path(self.config["local_path"]), data["target"])
                        parts = data["target"].split("/")
                        if parts[0].casefold().startswith(".gdrivecopy-"):
                            raise ValueError(
                                "Name conflicts with reserved partial-download directories"
                            )
                        for i in range(1, len(parts) + 1):
                            spelling = "/".join(parts[:i])
                            key = spelling.casefold()
                            kind = "file" if i == len(parts) else "folder"
                            previous = db.execute(
                                "SELECT spelling,kind FROM target_nodes WHERE name=?", (key,)
                            ).fetchone()
                            if previous and (
                                previous["spelling"] != spelling or previous["kind"] != kind
                            ):
                                db.execute(
                                    "INSERT OR IGNORE INTO target_conflicts VALUES(?)", (key,)
                                )
                            db.execute(
                                "INSERT OR IGNORE INTO target_nodes VALUES(?,?,?)",
                                (key, spelling, kind),
                            )
                        previous = db.execute(
                            "SELECT path FROM targets WHERE name=?", (key,)
                        ).fetchone()
                        if previous and previous["path"] != row["path"]:
                            db.execute("INSERT OR IGNORE INTO target_conflicts VALUES(?)", (key,))
                            db.execute(
                                "UPDATE files SET status='conflict',error='Unsafe namespace: destination collision' WHERE path=?",
                                (row["path"],),
                            )
                        db.execute("INSERT OR IGNORE INTO targets VALUES(?,?)", (key, row["path"]))
                    except (ValueError, OSError) as exc:
                        db.execute(
                            "UPDATE files SET status='conflict',error=? WHERE path=?",
                            ("Unsafe namespace: " + safe_error(exc), row["path"]),
                        )
                    metadata = data["remote"]
                    if not data.get("export_mime") and (
                        metadata["mimeType"].startswith("application/vnd.google-apps.")
                        or metadata.get("size") is None
                    ):
                        db.execute(
                            "UPDATE files SET status='unsupported',error=? WHERE path=? AND status!='conflict'",
                            (
                                "Native item needs supported export (--export-docs office/pdf); shortcuts are not followed",
                                row["path"],
                            ),
                        )
        for collision in self.store.rows("SELECT name FROM target_conflicts"):
            prefix = collision["name"] + "/"
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE files SET status='conflict',error='Unsafe namespace: destination collision' "
                    "WHERE path IN (SELECT path FROM targets WHERE name=? OR (name>=? AND name<?))",
                    (collision["name"], prefix, collision["name"] + "0"),
                )

    def _reconcile_completed(self) -> None:
        self.store.set("fatal_error", None)
        for row in self.store.rows(
            "SELECT * FROM files WHERE status IN ('copied','copied_unverified','exported') ORDER BY path"
        ):
            self.control.check()
            valid = False
            if self.config["direction"] == "upload":
                remote = self.inventory.at(row["path"])
                proof = json.loads(row["proof"] or "{}")
                if remote:
                    metadata = json.loads(remote["data"])
                    valid = (
                        remote["id"] == row["remote_id"]
                        and str(metadata.get("size")) == str(row["size"])
                        and (not proof.get("md5") or metadata.get("md5Checksum") == proof["md5"])
                    )
            else:
                data = json.loads(row["data"])
                target = safe_target(Path(self.config["local_path"]), data["target"])
                proof = json.loads(row["proof"] or "{}")
                if target.is_file():
                    stat = target.stat()
                    valid = stat.st_size == row["size"] and stat.st_mtime_ns == proof.get(
                        "mtime_ns"
                    )
            if not valid:
                self.store.update_file(row["path"], status="pending", error=None)

    def _pending(self):
        after = ""
        while True:
            with self.store._lock:
                batch = self.store.db.execute(
                    "SELECT * FROM files WHERE status='pending' AND path>? ORDER BY path LIMIT 500",
                    (after,),
                ).fetchall()
            if not batch:
                return
            for row in batch:
                after = row["path"]
                yield dict(row)

    def _dispatch(self, root_id: str) -> None:
        config = self.config
        limiter = _BandwidthLimiter(config["bwlimit"])
        uploader = Uploader(
            UploadConfig(
                source_dir=Path(config["local_path"]),
                drive_folder_id=root_id,
                transfers=config["transfers"],
                chunk_size=config["chunk_size"],
                bwlimit=config["bwlimit"],
                verify_checksum=config["verification"] == "checksum",
                retries=config["retries"],
            ),
            self.drive,
            control=self.control,
            sessions=DatabaseSessions(self.store),
            folder_map=FolderMap(self.store),
            reserve_id=self._reserve_id,
            discard_id=self._discard_id,
        )
        downloader = Downloader(self.store, self.drive, self.control, config, limiter)
        iterator = iter(self._pending())
        pending = {}
        transient_failures = 0
        with ThreadPoolExecutor(max_workers=config["transfers"]) as pool:
            try:
                while True:
                    while len(pending) < config["transfers"] and not self.control.stop.is_set():
                        row = next(iterator, None)
                        if row is None:
                            break
                        self.store.update_file(row["path"], status="running")
                        pending[pool.submit(self._one, row, uploader, downloader)] = row
                    if not pending:
                        self.control.check()
                        return
                    done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                    for future in done:
                        row = pending.pop(future)
                        try:
                            result = future.result()
                            self._bytes += result.get("bytes", 0)
                            self._resumed += result.get("resumed_bytes", 0)
                            self.store.update_file(
                                row["path"],
                                status=result["status"],
                                error=result.get("error"),
                                # Conflicts must retain prior receipts, including
                                # across repeated resumes of an edited upload.
                                **(
                                    {"proof": json.dumps(result["proof"])}
                                    if "proof" in result
                                    else {}
                                ),
                            )
                            event = (
                                "done"
                                if result["status"].startswith("copied")
                                or result["status"] == "exported"
                                else (
                                    "skip"
                                    if result["status"].startswith("skipped")
                                    else (
                                        "planned" if result["status"] == "pending" else "conflict"
                                    )
                                )
                            )
                            self.control.emit(
                                event,
                                row["path"],
                                size=row["size"],
                                message=result.get("error", ""),
                                **result,
                            )
                            transient_failures = 0
                        except Cancelled as exc:
                            self.store.update_file(row["path"], status="cancelled")
                            if not self.control.stop.is_set():
                                self.control.cancel(str(exc) or "Transfer interrupted")
                        except QuotaLimitError as exc:
                            self.store.update_file(
                                row["path"], status="pending", error=safe_error(exc)
                            )
                            self.control.cancel(
                                "Drive quota reached; resume after the applicable limit resets"
                            )
                        except Exception as exc:
                            message = safe_error(exc)
                            self.store.update_file(row["path"], status="failed", error=message)
                            self.control.emit(
                                "error", row["path"], message=message, size=row["size"]
                            )
                            transient_failures += 1
                            if transient_failures >= 5:
                                self.control.cancel(
                                    "Repeated transfer failures; inspect the report before resuming"
                                )
            except BaseException:
                if not self.control.stop.is_set():
                    self.control.cancel()
                raise

    def _one(self, row: dict, uploader: Uploader, downloader: Downloader) -> dict:
        self.control.check()
        self.control.emit("start", row["path"], size=row["size"])
        if self.config["direction"] == "upload":
            data = json.loads(row["data"])
            data["path"] = Path(data["path"])
            local = LocalFile(**data)
            uploader._assert_source_unchanged(local)
            remote = self.inventory.at(row["path"])
            if row["remote_id"] and not self.config["dry_run"]:
                # A saved create ID must never make a moved/trashed file appear
                # successfully copied to this job's destination.
                try:
                    reserved = self.drive.file_metadata(row["remote_id"])
                except DriveApiError as exc:
                    if exc.status != 404:
                        raise
                    reserved = None
                if reserved and reserved.get("trashed"):
                    self._discard_id(local)
                    row["remote_id"] = None
                elif reserved:
                    parent_path = row["path"].rpartition("/")[0]
                    parent = self.inventory.at(parent_path)
                    if (
                        not parent
                        or parent["id"] not in reserved.get("parents", [])
                        or reserved.get("name") != local.path.name
                    ):
                        return {
                            "status": "conflict",
                            "error": "Previously created Drive item moved or was renamed; review it before starting a new job",
                        }
                    proof = json.loads(row["proof"] or "null")
                    if proof and (
                        str(reserved.get("size")) != str(proof["size"])
                        or (proof.get("md5") and reserved.get("md5Checksum") != proof["md5"])
                    ):
                        # This item was already accepted in a previous run.
                        # A later edit must not enter failed-upload cleanup.
                        return {
                            "status": "conflict",
                            "error": "Previously copied Drive content changed; review it before starting a new job",
                        }
            if remote and remote["id"] != row["remote_id"]:
                if remote["folder"]:
                    return {"status": "conflict", "error": "Drive folder occupies file path"}
                metadata = json.loads(remote["data"])
                if str(metadata.get("size")) != str(local.size):
                    return {
                        "status": "conflict",
                        "error": "Existing Drive file has a different or unknown size",
                    }
                if self.config["existing"] == "checksum":
                    digest = uploader._file_md5(local.path)
                    uploader._assert_source_unchanged(local)
                    if digest != metadata.get("md5Checksum"):
                        return {
                            "status": "conflict",
                            "error": "Existing Drive checksum differs or is unavailable",
                        }
                    return {"status": "skipped_verified"}
                return {"status": "skipped_size"}
            parts = row["path"].split("/")
            for i in range(1, len(parts)):
                parent = self.inventory.at("/".join(parts[:i]))
                if parent and not parent["folder"]:
                    return {"status": "conflict", "error": "Drive file blocks a parent directory"}
            if self.config["dry_run"]:
                return {"status": "pending"}
            result = uploader.upload_file(local)
            if not result.success:
                raise RuntimeError(result.error)
            if result.file_id != self.store.file(row["path"])["remote_id"]:
                raise ValueError(
                    "Drive returned a different upload identity than the one reserved for this job"
                )
            if self.config["verification"] == "size":
                metadata = self.drive.file_metadata(result.file_id)
                if metadata.get("trashed") or str(metadata.get("size")) != str(local.size):
                    raise ValueError("Drive did not confirm the expected uploaded file size")
            proof = {
                "md5": result.md5_checksum,
                "size": local.size,
                "verified": self.config["verification"] == "checksum",
            }
            return {
                "status": "copied" if proof["verified"] else "copied_unverified",
                "bytes": result.bytes_uploaded,
                "resumed_bytes": local.size - result.bytes_uploaded if result.resumed else 0,
                "proof": proof,
            }
        source = json.loads(row["data"])
        target = safe_target(Path(self.config["local_path"]), source["target"])
        if target.exists():
            proof = json.loads(row["proof"] or "null")
            if not target.is_file() or target.stat().st_size != row["size"]:
                return {
                    "status": "conflict",
                    "error": "Existing local path has a different size or type",
                }
            if source.get("export_mime") and not proof:
                return {
                    "status": "conflict",
                    "error": "Existing export has no receipt; content equality cannot be established",
                }
            if (
                proof
                and not proof.get("verified")
                and not source.get("export_mime")
                and target.stat().st_mtime_ns == proof.get("mtime_ns")
            ):
                return {"status": "copied_unverified", "proof": proof}
            # Older jobs store absent receipts as the JSON string 'null', which
            # is truthy in Python. Only an actual receipt justifies rehashing a
            # size-only skip on resume.
            if self.config["existing"] == "checksum" or proof:
                hasher = (
                    hashlib.sha256()
                    if source.get("export_mime")
                    else hashlib.md5(usedforsecurity=False)
                )
                before = target.stat()
                with target.open("rb") as stream:
                    while block := stream.read(8 * 1024 * 1024):
                        self.control.check()
                        hasher.update(block)
                after = target.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ino,
                ):
                    raise ValueError("Local destination changed during verification")
                expected = (
                    proof.get("sha256")
                    if source.get("export_mime")
                    else source["remote"].get("md5Checksum")
                )
                if hasher.hexdigest() != expected:
                    return {
                        "status": "conflict",
                        "error": "Existing local checksum differs or is unavailable",
                    }
                if (proof and proof.get("verified")) or source.get("export_mime"):
                    proof["mtime_ns"] = after.st_mtime_ns
                    return {
                        "status": "exported" if source.get("export_mime") else "copied",
                        "proof": proof,
                    }
                return {"status": "skipped_verified"}
            return {"status": "skipped_size"}
        if self.config["dry_run"]:
            return {"status": "pending"}
        return downloader.transfer(row)

    def _write_file_report(self) -> None:
        import csv

        def cell(value):
            value = str(value or "")
            return (
                "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value
            )

        with atomic_text_writer(self.store.directory / "files.csv", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["path", "target", "bytes", "status", "error"])
            for row in self.store.rows(
                "SELECT path,data,size,status,error FROM files ORDER BY path"
            ):
                # Prefix formula-like untrusted filenames for spreadsheet safety.
                target = json.loads(row["data"]).get("target", row["path"])
                writer.writerow(
                    [
                        cell(row["path"]),
                        cell(target),
                        row["size"],
                        row["status"],
                        cell(row["error"]),
                    ]
                )


def build_report(store: JobStore) -> dict:
    config = store.get("config")
    counts = store.counts()
    errors = list(
        store.rows(
            "SELECT path,status,error FROM files WHERE error IS NOT NULL ORDER BY path LIMIT 100"
        )
    )
    limitations = [
        "Same-size skips do not establish content equality; use --existing checksum to hash existing files.",
        "Resume reuses the saved source manifest. Start a new copy job to include newly added files.",
        "A Drive listing is not a transactional snapshot; keep source/destination stable while copying.",
        "Resuming a partial checksum-verified file rereads its local prefix, without retransferring that prefix.",
        "Completed local copies are reused by size and modification time on resume; silent disk corruption is not rechecked. A new job with --existing checksum rehashes them.",
        "Empty directories, permissions, revision history, comments, and sharing settings are not copied.",
    ]
    if config["verification"] != "checksum":
        limitations.append(
            "Checksum verification was disabled; copied_unverified files were checked by size only."
        )
    if counts.get("unsupported"):
        limitations.append(
            "Some Google-native items/shortcuts were not copied. Select --export-docs for supported documents in a new job."
        )
    if config.get("export_docs"):
        limitations.append(
            "Native document exports are format conversions with Google's 10 MB limit, no range resume, and no source MD5; they are counted separately."
        )
    return {
        "schema_version": 1,
        "job_id": config["job_id"],
        "status": store.get("status", "new"),
        "direction": config["direction"],
        "account_email": config["account_email"],
        "source": config["source"],
        "destination": config["destination"],
        "counts": counts,
        "bytes_this_run": store.get("bytes_this_run", 0),
        "resumed_bytes_this_run": store.get("resumed_bytes_this_run", 0),
        "bytes_total_runs": store.get("bytes_total_runs", 0),
        "duration_seconds": store.get("duration_seconds", 0),
        "average_bytes_per_second": store.get("bytes_this_run", 0)
        / max(store.get("duration_seconds", 0), 0.001),
        "seconds_total_runs": store.get("seconds_total_runs", 0),
        "created": store.get("created"),
        "last_started": store.get("last_started"),
        "last_finished": store.get("last_finished"),
        "scan_errors": store.get("scan_errors", 0),
        "scan_errors_sample": store.get("scan_errors_sample", []),
        "symlinks_skipped": store.get("symlinks_skipped", 0),
        "tool_entries_excluded": store.get("tool_entries_excluded", 0),
        "fatal_error": store.get("fatal_error"),
        "stop_reason": store.get("stop_reason"),
        "errors_sample": errors,
        "limitations": limitations,
        "directory": str(store.directory),
        "state_dir": config["state_dir"],
        "avoided_bytes": sum(
            counts.get(s, {}).get("bytes", 0) for s in ("skipped_size", "skipped_verified")
        ),
        "retries": next(store.rows("SELECT COUNT(*) AS n FROM events WHERE kind='retry'"))["n"],
    }
