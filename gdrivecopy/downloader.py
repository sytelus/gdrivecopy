"""Verified, ranged downloads with durable local checkpoints and no-clobber publication."""

from __future__ import annotations

import hashlib
import json
import os
import random
from datetime import datetime
from pathlib import Path

from requests.exceptions import RequestException

from gdrivecopy.control import RunControl
from gdrivecopy.drive import DriveApiError, QuotaLimitError, RateLimitError
from gdrivecopy.jobstore import JobStore
from gdrivecopy.scanner import _is_link_like

WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    # Win32 also recognizes superscript 1, 2 and 3 as device-number aliases.
    *(f"COM{i}" for i in "123456789¹²³"),
    *(f"LPT{i}" for i in "123456789¹²³"),
}


def is_link(path: Path) -> bool:
    return path.is_symlink() or (path.exists() and _is_link_like(path))


def safe_target(root: Path, relative: str, *, create_parents: bool = False) -> Path:
    """Reject path traversal, Windows aliases, and links before writing anything."""
    parts = relative.split("/")
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or part.rstrip(" .") != part
            or any(c in '<>:"\\|?*' or ord(c) < 32 for c in part)
            or part.split(".")[0].upper() in WINDOWS_RESERVED
        ):
            raise ValueError(f"Drive name cannot be represented safely locally: {relative!r}")
    current = root
    if create_parents:
        root.mkdir(parents=True, exist_ok=True)
    if is_link(current):
        raise ValueError("Destination root cannot be a symlink or junction")
    for part in parts[:-1]:
        current = current / part
        if create_parents:
            current.mkdir(exist_ok=True)
        if is_link(current) or (current.exists() and not current.is_dir()):
            raise ValueError(f"Unsafe destination directory: {current}")
    target = current / parts[-1]
    if is_link(target):
        raise ValueError(f"Destination path is a link: {target}")
    return target


def metadata_signature(metadata: dict) -> tuple:
    return (
        metadata.get("id"),
        str(metadata.get("size")),
        metadata.get("md5Checksum"),
        metadata.get("version"),
        metadata.get("modifiedTime"),
    )


class Downloader:
    def __init__(self, store: JobStore, drive, control: RunControl, config: dict, limiter) -> None:
        self.store, self.drive, self.control, self.config = store, drive, control, config
        self.root = Path(config["local_path"])
        self.parts = self.root / f".gdrivecopy-{config['job_id']}.parts"
        self.limiter = limiter

    def transfer(self, row: dict) -> dict:
        path = row["path"]
        source = json.loads(row["data"])
        metadata = source["remote"]
        target = safe_target(self.root, source.get("target", path), create_parents=True)
        if target.exists():
            raise FileExistsError(f"Destination appeared after planning: {target}")
        self.parts.mkdir(exist_ok=True)
        if _is_link_like(self.parts):
            raise ValueError("Partial download directory cannot be a link")
        partial = self.parts / (hashlib.sha256(path.encode()).hexdigest() + ".part")
        if is_link(partial):
            raise ValueError("Partial download is a link")

        fresh = self.drive.file_metadata(metadata["id"])
        if fresh.get("trashed") or metadata_signature(fresh) != metadata_signature(metadata):
            raise ValueError("Drive source changed since planning; create a new copy job")
        if not fresh.get("capabilities", {}).get("canDownload", True):
            raise ValueError("This account does not have permission to download the file")

        if source.get("export_mime"):
            return self._export(row, source, partial, target)

        expected_md5 = metadata.get("md5Checksum")
        verify = self.config["verification"] == "checksum"
        if verify and not expected_md5:
            raise ValueError(
                "Drive does not provide an MD5 for this item; no verified binary copy is possible"
            )
        total = row["size"]
        offset = min(row["offset"], total)
        if not partial.exists():
            offset = 0
        elif partial.stat().st_size < offset:
            offset = 0  # A checkpoint may outlive damaged/missing local data.
        mode = "r+b" if partial.exists() else "x+b"
        hasher = hashlib.md5(usedforsecurity=False) if verify else None
        resumed_bytes = offset
        transferred = 0
        with partial.open(mode) as stream:
            # Bytes beyond the durable DB checkpoint may not have reached disk
            # before a crash. Discard them and request that range again.
            stream.truncate(offset)
            if hasher and offset:
                remaining = offset
                while remaining:
                    self.control.check()
                    block = stream.read(min(8 * 1024 * 1024, remaining))
                    if not block:
                        raise OSError("Partial file became shorter while verifying resume data")
                    hasher.update(block)
                    remaining -= len(block)
                    self.control.emit(
                        "progress",
                        path,
                        offset=offset - remaining,
                        size=total,
                        status="Checking resume data",
                    )
            stream.seek(offset)
            self.control.emit("progress", path, offset=offset, size=total, status="Downloading")
            while offset < total:
                self.control.check()
                size = min(self.config["chunk_size"], total - offset)
                self.limiter.wait_for_slot(size, self.control.stop)
                self.control.check()
                data = self._range(metadata["id"], offset, size, total, path)
                stream.write(data)
                if hasher:
                    hasher.update(data)
                stream.flush()
                os.fsync(stream.fileno())
                offset += len(data)
                transferred += len(data)
                # Ordering is critical: sync bytes BEFORE recording their offset.
                self.store.update_file(path, offset=offset)
                self.control.emit(
                    "progress",
                    path,
                    offset=offset,
                    size=total,
                    bytes=len(data),
                    status="Downloading",
                )
            stream.flush()
            os.fsync(stream.fileno())

        actual_md5 = hasher.hexdigest() if hasher else None
        if verify and actual_md5 != expected_md5:
            # Keep neither a corrupt final path nor a bad resume prefix.
            self.store.update_file(path, offset=0)
            raise ValueError(
                "Downloaded checksum does not match Drive; partial will restart on resume"
            )
        after = self.drive.file_metadata(metadata["id"])
        if after.get("trashed") or metadata_signature(after) != metadata_signature(metadata):
            self.store.update_file(path, offset=0)
            raise ValueError("Drive source changed during download; final path was not published")

        if metadata.get("modifiedTime"):
            timestamp = datetime.fromisoformat(
                metadata["modifiedTime"].replace("Z", "+00:00")
            ).timestamp()
            os.utime(partial, (timestamp, timestamp))
        # Persist proof BEFORE the rename. If the process dies between rename
        # and final status, recovery can verify the already-published file.
        proof = {
            "md5": actual_md5,
            "remote": metadata_signature(metadata),
            "size": total,
            "mtime_ns": partial.stat().st_mtime_ns,
            "verified": verify,
        }
        self.store.update_file(path, proof=json.dumps(proof))
        self._publish(partial, target, source["target"])
        return {
            "status": "copied" if verify else "copied_unverified",
            "bytes": transferred,
            "resumed_bytes": resumed_bytes,
            "proof": proof,
        }

    def _publish(self, partial: Path, target: Path, relative: str) -> None:
        self.control.check()
        safe_target(self.root, relative, create_parents=False)
        if os.name == "nt":
            os.rename(partial, target)  # Windows rename refuses an existing destination.
        else:
            os.link(partial, target)  # POSIX rename would overwrite; link is atomic/no-clobber.
            partial.unlink()
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _export(self, row: dict, source: dict, partial: Path, target: Path) -> dict:
        """Native exports are bounded conversions, without a source checksum."""
        metadata = source["remote"]
        data = self._request(
            lambda: self.drive.export_document(metadata["id"], source["export_mime"]), row["path"]
        )
        self.limiter.wait_for_slot(len(data), self.control.stop)
        self.control.check()
        after = self.drive.file_metadata(metadata["id"])
        if after.get("trashed") or metadata_signature(after) != metadata_signature(metadata):
            raise ValueError("Google document changed during export; final path was not published")
        with partial.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        proof = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mtime_ns": partial.stat().st_mtime_ns,
            "verified": False,
            "remote": metadata_signature(metadata),
            "export_mime": source["export_mime"],
        }
        self.store.update_file(row["path"], size=len(data), proof=json.dumps(proof))
        self.control.emit(
            "progress",
            row["path"],
            offset=len(data),
            size=len(data),
            bytes=len(data),
            status="Exported",
        )
        self._publish(partial, target, source["target"])
        return {"status": "exported", "bytes": len(data), "proof": proof}

    def _range(self, file_id: str, offset: int, size: int, total: int, path: str) -> bytes:
        return self._request(lambda: self.drive.download_range(file_id, offset, size, total), path)

    def _request(self, request, path: str) -> bytes:
        refreshed = False
        for attempt in range(self.config["retries"]):
            self.control.check()
            try:
                return request()
            except QuotaLimitError:
                raise
            except (DriveApiError, RequestException) as exc:
                if isinstance(exc, DriveApiError):
                    if exc.status == 401 and not refreshed:
                        self.drive.refresh_credentials()
                        refreshed = True
                        continue
                    if not isinstance(exc, RateLimitError) and exc.status not in {
                        408,
                        429,
                        500,
                        502,
                        503,
                        504,
                    }:
                        raise
                if attempt + 1 == self.config["retries"]:
                    raise
                delay = random.uniform(0, min(60, 2**attempt))
                self.control.emit(
                    "retry", path, attempt=attempt + 1, delay=delay, message=type(exc).__name__
                )
                self.control.wait(delay)
        raise DriveApiError(401 if refreshed else 503, "Download retry budget exhausted")
