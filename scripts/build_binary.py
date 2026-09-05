"""Build, smoke-test, and archive a native, self-contained terminal application.

Run from an isolated environment with .[build] installed. Builds target the
current OS/architecture; PyInstaller is not a cross-compiler.
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def runtime_distributions():
    """Collect declared runtime dependencies, excluding dev/build extras."""
    pending, seen = ["gdrivecopy"], set()
    while pending:
        name = canonicalize_name(pending.pop())
        if name in seen:
            continue
        seen.add(name)
        dist = metadata.distribution(name)
        yield dist
        for text in dist.requires or []:
            requirement = Requirement(text)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)


def write_notices(bundle: Path) -> None:
    notices = bundle / "licenses"
    notices.mkdir(exist_ok=True)
    versions = {}
    # The frozen executable also redistributes PyInstaller's bootloader, whose
    # license/exception is required even though it is not an app dependency.
    for dist in [*runtime_distributions(), metadata.distribution("pyinstaller")]:
        name = canonicalize_name(dist.metadata["Name"])
        versions[name] = dist.version
        texts = []
        for entry in dist.files or []:
            if entry.name.lower().startswith(("license", "licence", "copying", "notice")):
                path = Path(dist.locate_file(entry))
                if path.is_file():
                    texts.append(
                        f"--- {entry.name} ---\n{path.read_text(encoding='utf-8', errors='replace')}"
                    )
        if not texts:
            texts.append(
                dist.metadata.get("License-Expression")
                or dist.metadata.get("License")
                or f"See project metadata: {dist.metadata.get('Home-page', '')}"
            )
        (notices / f"{name}.txt").write_text("\n\n".join(texts), encoding="utf-8")
    # Include the exact interpreter's license, independent of OS installer layout.
    url = f"https://raw.githubusercontent.com/python/cpython/v{platform.python_version()}/LICENSE"
    with urllib.request.urlopen(url, timeout=30) as response:
        (notices / "python.txt").write_bytes(response.read())
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None  # Source archives have no .git directory.
    info = {
        "version": metadata.version("gdrivecopy"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "commit": commit,
        "dependencies": dict(sorted(versions.items())),
    }
    (bundle / "BUILD_INFO.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    import PyInstaller.__main__

    output = ROOT / "dist"
    work = ROOT / "build" / "native"
    work.mkdir(parents=True, exist_ok=True)
    # One-folder bundles start promptly and do not depend on an extraction
    # directory surviving a days-long transfer. Keep support files together.
    PyInstaller.__main__.run(
        [
            "--noconfirm",
            "--clean",
            "--onedir",
            "--console",
            "--noupx",
            "--name",
            "gdrivecopy",
            "--distpath",
            str(output),
            "--workpath",
            str(work),
            "--specpath",
            str(work),
            "--paths",
            str(ROOT),
            "--collect-data",
            "googleapiclient",
            "--copy-metadata",
            "google-api-python-client",
            str(ROOT / "gdrivecopy" / "__main__.py"),
        ]
    )
    bundle = output / "gdrivecopy"
    for name in (
        "LICENSE",
        "README.md",
        "USAGE.md",
        "OPERATIONS.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "DEVELOPMENT.md",
        "PRD.md",
    ):
        shutil.copy2(ROOT / name, bundle / name)
    write_notices(bundle)
    executable = bundle / ("gdrivecopy.exe" if os.name == "nt" else "gdrivecopy")
    with tempfile.TemporaryDirectory(prefix="gdrivecopy-smoke-") as temporary:
        for args in (["--version"], ["copy", "--help"], ["doctor", "--json"]):
            subprocess.run([str(executable), *args], check=True, cwd=temporary, timeout=90)
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}[platform.system()]
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    archive = shutil.make_archive(
        str(output / f"gdrivecopy-{system}-{machine}"),
        "zip" if os.name == "nt" else "gztar",
        output,
        "gdrivecopy",
    )
    print(f"Built and checked {archive}")


if __name__ == "__main__":
    main()
