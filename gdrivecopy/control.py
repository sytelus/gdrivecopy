"""Cooperative cancellation and a bounded, thread-safe live progress model."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


class Cancelled(Exception):
    """The current transfer should stop at its next safe checkpoint."""


@dataclass
class RunControl:
    """Shared by metadata requests, file workers, retry waits, and the dashboard."""

    stop: threading.Event = field(default_factory=threading.Event)
    on_event: Callable[..., None] | None = None
    reason: str = "Cancelled by user"

    def cancel(self, reason: str = "Cancelled by user") -> None:
        self.reason = reason
        self.stop.set()

    def check(self) -> None:
        if self.stop.is_set():
            raise Cancelled(self.reason)

    def wait(self, seconds: float) -> None:
        if self.stop.wait(seconds):
            raise Cancelled(self.reason)

    def emit(self, kind: str, path: str = "", **details: object) -> None:
        if self.on_event is not None:
            self.on_event(kind, path, **details)


class ProgressModel:
    """Keep only active files and recent errors; never accumulate the file tree."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.phase = "Connecting"
        self.active: dict[str, dict] = {}
        self.errors: deque[str] = deque(maxlen=5)
        self.started = time.monotonic()
        self.samples: deque[tuple[float, int]] = deque(maxlen=256)
        self.wire_bytes = 0
        self.total_bytes = 0
        self.finished_bytes = 0
        self.total_files = 0
        self.finished_files = 0
        self.retries = 0
        self.scanned = 0

    def event(self, kind: str, path: str = "", **data: object) -> None:
        with self.lock:
            if kind == "phase":
                self.phase = str(data["phase"])
            elif kind == "plan":
                self.total_files = int(data["files"])
                self.total_bytes = int(data["bytes"])
                self.finished_files = int(data.get("finished_files", 0))
                self.finished_bytes = int(data.get("finished_bytes", 0))
            elif kind == "scan":
                self.scanned += int(data.get("count", 1))
            elif kind == "start":
                self.active[path] = {
                    "size": int(data.get("size", 0)),
                    "offset": 0,
                    "status": "Starting",
                }
            elif kind == "progress":
                item = self.active.setdefault(path, {"size": int(data.get("size", 0))})
                if not item["size"] and data.get("size"):
                    item["size"] = int(data["size"])
                    self.total_bytes += item["size"]
                item.update(
                    offset=int(data["offset"]), status=str(data.get("status", "Transferring"))
                )
                self.wire_bytes += int(data.get("bytes", 0))
                now = time.monotonic()
                self.samples.append((now, self.wire_bytes))
                while len(self.samples) > 2 and self.samples[0][0] < now - 30:
                    self.samples.popleft()
            elif kind == "retry":
                self.retries += 1
                if path in self.active:
                    self.active[path]["status"] = "Retrying"
            elif kind in {"done", "skip", "conflict", "error", "unsupported", "planned"}:
                item = self.active.pop(path, {})
                self.finished_files += 1
                self.finished_bytes += max(int(data.get("size", 0)), item.get("size", 0))
                if kind in {"error", "conflict", "unsupported"}:
                    self.errors.append(f"{path}: {data.get('message', kind)}")

    def snapshot(self) -> dict:
        with self.lock:
            now = time.monotonic()
            rate = 0.0
            if len(self.samples) > 1:
                first, last = self.samples[0], self.samples[-1]
                rate = (last[1] - first[1]) / max(now - first[0], 0.001)
            in_flight = sum(item.get("offset", 0) for item in self.active.values())
            remaining = max(0, self.total_bytes - self.finished_bytes - in_flight)
            return {
                "phase": self.phase,
                "active": {k: dict(v) for k, v in self.active.items()},
                "errors": list(self.errors),
                "elapsed": now - self.started,
                "rate": rate,
                "eta": remaining / rate if rate else None,
                "wire_bytes": self.wire_bytes,
                "total_bytes": self.total_bytes,
                "finished_bytes": self.finished_bytes,
                "total_files": self.total_files,
                "finished_files": self.finished_files,
                "retries": self.retries,
                "scanned": self.scanned,
            }
