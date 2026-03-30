"""Tests for gdrivecopy.session -- session cache for upload resume."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from gdrivecopy.models import SessionEntry
from gdrivecopy.session import SessionCache


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


class TestSessionCacheLoad:
    def test_load_missing_file(self, session_file: Path) -> None:
        """Loading when the file does not exist leaves the cache empty."""
        cache = SessionCache(session_file)
        cache.load()
        assert len(cache) == 0

    def test_load_valid_file(self, populated_session_file: Path) -> None:
        """Loading a well-formed JSON file populates the cache."""
        cache = SessionCache(populated_session_file)
        cache.load()
        assert len(cache) == 1

        entry = cache.get("photos/img001.jpg")
        assert entry is not None
        assert entry.session_uri == "https://upload.example.com/session1"
        assert entry.file_size == 1024
        assert entry.mtime == "2025-01-01T00:00:00+00:00"

    def test_load_corrupt_json(self, session_file: Path) -> None:
        """A corrupt JSON file is ignored (cache stays empty, no crash)."""
        session_file.write_text("{invalid json!!!", encoding="utf-8")
        cache = SessionCache(session_file)
        cache.load()
        assert len(cache) == 0

    def test_load_missing_keys(self, session_file: Path) -> None:
        """JSON with missing required keys is treated as corrupt."""
        session_file.write_text(
            json.dumps({"key": {"session_uri": "x"}}),  # missing file_size, mtime
            encoding="utf-8",
        )
        cache = SessionCache(session_file)
        cache.load()
        assert len(cache) == 0

    def test_load_empty_object(self, session_file: Path) -> None:
        """An empty JSON object results in an empty cache."""
        session_file.write_text("{}", encoding="utf-8")
        cache = SessionCache(session_file)
        cache.load()
        assert len(cache) == 0


class TestSessionCacheSave:
    def test_save_creates_file(self, session_file: Path) -> None:
        """save() writes the cache to disk even when the file did not exist."""
        cache = SessionCache(session_file)
        cache.put("a/b.txt", SessionEntry("uri1", 100, "2025-01-01T00:00:00+00:00"))
        assert session_file.exists()

    def test_save_round_trips(self, session_file: Path) -> None:
        """Data saved can be loaded back correctly."""
        entry = SessionEntry("uri_x", 999, "2025-06-15T12:00:00+00:00")
        cache1 = SessionCache(session_file)
        cache1.put("doc/report.pdf", entry)

        cache2 = SessionCache(session_file)
        cache2.load()
        loaded = cache2.get("doc/report.pdf")
        assert loaded is not None
        assert loaded.session_uri == "uri_x"
        assert loaded.file_size == 999
        assert loaded.mtime == "2025-06-15T12:00:00+00:00"

    def test_save_is_atomic(self, session_file: Path) -> None:
        """save() writes to a .tmp file then renames, so .tmp should not linger."""
        cache = SessionCache(session_file)
        cache.put("f.txt", SessionEntry("u", 1, "t"))
        assert not session_file.with_suffix(".tmp").exists()


# ---------------------------------------------------------------------------
# Get / put / remove
# ---------------------------------------------------------------------------


class TestSessionCacheAccess:
    def test_get_missing_key_returns_none(self, session_file: Path) -> None:
        """get() for a non-existent key returns None."""
        cache = SessionCache(session_file)
        assert cache.get("no/such/key") is None

    def test_put_then_get(self, session_file: Path) -> None:
        """put() followed by get() returns the same entry."""
        cache = SessionCache(session_file)
        entry = SessionEntry("uri_a", 42, "2025-01-01T00:00:00+00:00")
        cache.put("x.txt", entry)
        assert cache.get("x.txt") is entry

    def test_put_overwrites_existing(self, session_file: Path) -> None:
        """put() with the same key replaces the previous entry."""
        cache = SessionCache(session_file)
        cache.put("x.txt", SessionEntry("old", 1, "t1"))
        cache.put("x.txt", SessionEntry("new", 2, "t2"))
        result = cache.get("x.txt")
        assert result is not None
        assert result.session_uri == "new"

    def test_remove_existing_key(self, session_file: Path) -> None:
        """remove() deletes the entry and persists the change."""
        cache = SessionCache(session_file)
        cache.put("x.txt", SessionEntry("uri", 1, "t"))
        cache.remove("x.txt")
        assert cache.get("x.txt") is None
        assert len(cache) == 0

    def test_remove_nonexistent_key_is_noop(self, session_file: Path) -> None:
        """remove() for a missing key does not raise."""
        cache = SessionCache(session_file)
        cache.remove("never_existed")  # must not raise

    def test_len_reflects_entries(self, session_file: Path) -> None:
        """__len__ reports the correct number of entries."""
        cache = SessionCache(session_file)
        assert len(cache) == 0
        cache.put("a", SessionEntry("u1", 1, "t"))
        cache.put("b", SessionEntry("u2", 2, "t"))
        assert len(cache) == 2
        cache.remove("a")
        assert len(cache) == 1


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestSessionCacheThreadSafety:
    def test_concurrent_puts(self, session_file: Path) -> None:
        """Multiple threads writing to the cache simultaneously must not crash."""
        cache = SessionCache(session_file)
        errors: list[Exception] = []

        def _writer(idx: int) -> None:
            try:
                for i in range(20):
                    cache.put(
                        f"thread{idx}/file{i}.bin",
                        SessionEntry(f"uri_{idx}_{i}", i, "t"),
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent put errors: {errors}"
        # All entries should be present.
        assert len(cache) == 4 * 20

    def test_concurrent_read_write(self, session_file: Path) -> None:
        """Reads interleaved with writes must not crash or corrupt data."""
        cache = SessionCache(session_file)
        cache.put("seed", SessionEntry("uri", 1, "t"))
        errors: list[Exception] = []

        def _reader() -> None:
            try:
                for _ in range(50):
                    cache.get("seed")
                    len(cache)
            except Exception as exc:
                errors.append(exc)

        def _writer() -> None:
            try:
                for i in range(50):
                    cache.put(f"w/{i}", SessionEntry("u", i, "t"))
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_reader)
        t2 = threading.Thread(target=_writer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == []
