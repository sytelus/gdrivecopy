This patch release improves copy recovery, diagnostic privacy, and error reporting.

## What's fixed

- Protect unrelated files and previously completed uploads from unsafe cleanup.
- Reject recovered folders that moved, changed, or went to trash.
- Rebuild interrupted plans and recover inventory cursors without losing changes.
- Preserve fast size-only skips on resume and pause cleanly on quota errors.
- Strengthen account/path validation and secret redaction in errors and logs.
- Show source scan failures, handle damaged job listings, and record accurate build provenance.

Adds 41 test cases. Source checks pass on Windows/Linux with Python 3.10 and 3.14;
native packages also run tests and offline installation checks on their build platforms.

## Upgrading from 0.2.0

Existing accounts and saved jobs remain compatible. Stop the app, extract the
new binary archive into its own folder, and resume the original job. Retain
the state directory and partial downloads; pass your original `--state-dir` if customized.

## Downloads

- Windows x64: `gdrivecopy-windows-x86_64.zip`
- Linux x64 (built on Ubuntu 22.04): `gdrivecopy-linux-x86_64.tar.gz`
- macOS Apple Silicon (built on macOS 14): `gdrivecopy-macos-arm64.tar.gz`
- Python 3.10+: wheel and source distribution are included.

Extract a native archive and keep the entire `gdrivecopy` folder together.
Run `.\gdrivecopy.exe --help` on Windows or `./gdrivecopy --help` on Linux/macOS.
Python is bundled. Current binaries are not publisher-signed or notarized.
`SHA256SUMS` covers the downloadable assets; each native bundle contains
dependency versions, build information, project docs and license notices.

## Quick use

Create a Desktop OAuth client with the Drive API enabled, then:

```sh
gdrivecopy accounts add personal --credentials "path/to/client.json"
gdrivecopy copy "D:/Photos" drive:FOLDER_ID --account personal
gdrivecopy copy drive:root "E:/Drive backup" --account personal
gdrivecopy resume JOB_ID
gdrivecopy doctor
```

For native builds, use the executable prefix described above. See the
[quick start](https://github.com/sytelus/gdrivecopy/tree/v0.2.1#readme),
[user guide](https://github.com/sytelus/gdrivecopy/blob/v0.2.1/USAGE.md), and
[changelog](https://github.com/sytelus/gdrivecopy/blob/v0.2.1/CHANGELOG.md).

## Integrity and compatibility

New binary copies default to checksum verification. Existing same-size files
are skipped without hashing unless `--existing checksum` is selected. Existing
content is never overwritten. Native Google document exports are conversions,
and shared drives, permissions/history and mirroring are not supported.

The old JSON-session interface is now `legacy-upload`; existing `sessions.json`
jobs are not imported into new SQLite jobs.

CI tests the source and checks native binaries' help, Drive discovery data, TLS
certificates, SQLite and local persistence without authenticating. These checks
do not validate live Google transfers, all consumer OS configurations, or
sustained 30 TB migrations. Review quota/recovery guidance before a large copy.
