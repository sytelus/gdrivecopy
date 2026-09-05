"""Replace-atomic writes shared by tokens, resumable state, and reports."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def write_text_atomic(path: Path, text: str) -> None:
    with atomic_text_writer(path) as stream:
        stream.write(text)


@contextmanager
def atomic_text_writer(path: Path, *, newline: str = "\n"):
    """Write privately from creation, then replace the destination atomically.

    A unique, exclusively created sibling avoids following a pre-existing
    temporary-file symlink. POSIX mode is 0600; Windows uses inherited ACLs.
    The containing directory must therefore be trusted on every platform.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
