# gdrivecopy

[![Tests](https://github.com/sytelus/gdrivecopy/actions/workflows/ci.yml/badge.svg)](https://github.com/sytelus/gdrivecopy/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/sytelus/gdrivecopy)](https://github.com/sytelus/gdrivecopy/releases/latest)
[![MIT license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Copy folders **to or from Google Drive** with a live terminal dashboard,
checksum verification, and progress you can resume after a reboot.

## 1. Get the app

**No Python needed:** download a [release](https://github.com/sytelus/gdrivecopy/releases/latest),
extract it, and open a terminal in the extracted `gdrivecopy` folder.

| Platform | Download | Run |
|---|---|---|
| Windows x64 | [ZIP](https://github.com/sytelus/gdrivecopy/releases/latest/download/gdrivecopy-windows-x86_64.zip) | `.\gdrivecopy.exe --help` |
| Linux x64 | [tar.gz](https://github.com/sytelus/gdrivecopy/releases/latest/download/gdrivecopy-linux-x86_64.tar.gz) | `./gdrivecopy --help` |
| macOS Apple Silicon | [tar.gz](https://github.com/sytelus/gdrivecopy/releases/latest/download/gdrivecopy-macos-arm64.tar.gz) | `./gdrivecopy --help` |

Keep the extracted folder together. Binaries are not publisher-signed/notarized;
see [installation notes](USAGE.md#release-binaries) if your OS blocks them.
Other platforms can install from source with **Python 3.10+**:

```sh
git clone https://github.com/sytelus/gdrivecopy.git
cd gdrivecopy
python -m pip install .
gdrivecopy --help
```

Installation builds the Python package automatically. To build a standalone
binary yourself: `python -m pip install ".[build]"`, then
`python scripts/build_binary.py`. See [build instructions](DEVELOPMENT.md#building-and-releasing).

## 2. Connect Google Drive

Create a **Desktop app OAuth client** in the [Google Cloud Console](https://console.cloud.google.com/),
enable the **Google Drive API**, and download its client JSON.
[Step-by-step setup](USAGE.md#install-and-sign-in).

```sh
gdrivecopy accounts add personal --credentials "path/to/client.json"
```

Choose your Google account in the browser. The app shows its verified email
before copying. Add more profiles and select them with `--account work`.

For an extracted binary, replace `gdrivecopy` in the examples below with
`.\gdrivecopy.exe` on Windows or `./gdrivecopy` on Linux/macOS.

## 3. Copy, pause, and resume

```sh
# Upload a local folder's contents
gdrivecopy copy "D:/Photos" drive:FOLDER_ID --account personal

# Download all of My Drive
gdrivecopy copy drive:root "E:/Drive backup" --account personal

# Preview a copy without transferring files
gdrivecopy copy drive:FOLDER_ID "E:/Backup" --dry-run

# After Ctrl+C or a reboot
gdrivecopy jobs
gdrivecopy resume JOB_ID

# Review results
gdrivecopy report JOB_ID
```

Find `FOLDER_ID` after `/folders/` in the folder's Drive URL. Copies are recursive
and **never overwrite existing files**. New binary copies are checksum-verified;
existing same-size files are skipped without hashing. Use `--existing checksum`
for a stronger comparison. Google Docs need `--export-docs office` or `pdf`.

**Scope:** My Drive only; no shared drives, mirroring, or permissions/history
backup. For multi-day jobs, read the [recovery and quota notes](OPERATIONS.md).
Full-scale 30 TB and live Google behavior still need integration validation.

## Help and development

[Command guide](USAGE.md) · [Operations](OPERATIONS.md) ·
[Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) ·
[Report a bug](https://github.com/sytelus/gdrivecopy/issues/new/choose) ·
[Security](SECURITY.md) · [MIT license](LICENSE)

Run `gdrivecopy copy --help` for options or `gdrivecopy doctor` for an offline
installation check. Developers: `python -m pip install -e ".[dev]"`, then
`python -m pytest`. Architecture and release details are in [DEVELOPMENT.md](DEVELOPMENT.md).
