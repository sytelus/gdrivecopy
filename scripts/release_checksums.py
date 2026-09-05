"""Generate SHA256SUMS for explicit release assets in a staging directory."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def main(directory: Path) -> None:
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name != "SHA256SUMS"
        and path.name.endswith((".zip", ".tar.gz", ".whl"))
    )
    if not files:
        raise SystemExit("No release assets found")
    lines = []
    for path in files:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  {path.name}\n")
    (directory / "SHA256SUMS").write_text("".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "dist"))
