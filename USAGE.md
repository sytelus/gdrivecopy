# User guide

For the shortest setup path, start with the [README](README.md).

This guide covers account setup, copy options, verification and migration from
the original uploader. See [OPERATIONS.md](OPERATIONS.md) for long-running jobs.

## Release binaries

Download native archives from [GitHub Releases](https://github.com/sytelus/gdrivecopy/releases/latest).
Windows x64 uses ZIP; Linux x64 and macOS Apple Silicon use tar.gz. Extract the
whole archive and retain the `_internal` directory beside the executable.
From the extracted `gdrivecopy` directory, run `.\gdrivecopy.exe` on Windows or
`./gdrivecopy` on Linux/macOS. Add that directory to your own PATH if you want
to run `gdrivecopy` from any directory; no administrator installation is needed.

Linux binaries are built on Ubuntu 22.04 and macOS binaries on macOS 14 ARM64.
For older systems or other architectures, use Python installation from source.
Windows/macOS binaries do not have publisher signing/notarization and may be
blocked by OS policy. Verify the origin/hash and follow your organization's
policy; source installation is an alternative. Do not disable OS protections.

Compare the download with the release's `SHA256SUMS`: use
`Get-FileHash .\gdrivecopy-windows-x86_64.zip -Algorithm SHA256` on PowerShell,
`sha256sum -c SHA256SUMS --ignore-missing` on Linux, or
`shasum -a 256 gdrivecopy-macos-arm64.tar.gz` on macOS. Compare the complete
hex digest, not just its beginning. Checksums do not replace publisher signatures.

Run `gdrivecopy doctor` to check bundled dependencies and local persistence
without signing in or sending a Google request. `doctor --json` is useful in
bug reports. It does not test live credentials, permissions or transfers.

A fast terminal app for copying folder trees **to and from Google Drive**, with
concurrent transfers, a live dashboard, checksum verification, and durable jobs
that continue after cancellation, a crash, or a reboot.

```powershell
gdrivecopy accounts add personal --credentials "C:\Private\client.json"
gdrivecopy copy "D:\Photos" drive:FOLDER_ID --account personal
gdrivecopy copy drive:root "E:\Drive backup" --account personal
gdrivecopy resume JOB_ID
```

The dashboard shows the actual account, active files, progress, transfer rate,
elapsed time, ETA, retries, and recent errors. `--no-progress` uses plain logs;
`--quiet` shows the final report only. Redirected output uses plain logs.
There is no browser interface or background service.

## Install and sign in

Requires Python 3.10+, an enabled Google Drive API project, and a Desktop OAuth
client JSON. This version supports **My Drive** folders; shared drives are rejected.

```powershell
git clone https://github.com/sytelus/gdrivecopy.git
cd gdrivecopy
python -m pip install .
gdrivecopy --help
```

In the [Google Cloud Console](https://console.cloud.google.com/), enable the
Google Drive API, configure the OAuth audience, and create a **Desktop app**
OAuth client. Download its JSON to a private location outside application state.
Pass that path to `accounts add`. The browser opens for account selection and
consent; subsequent transfers run in the terminal.

The app requests the full Drive scope to enumerate existing content. Tokens and
job databases are sensitive: a database can contain resumable upload URLs that
authorize continuation of an upload. Reports and audit exports omit these URLs.
State uses private files on POSIX and inherited directory permissions on Windows.

For multi-day jobs, review [Google's token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration).
External OAuth apps in Testing can have refresh tokens that expire after seven
days. Usable credentials refresh automatically; revoked/expired authorization
requires signing in again to the original account.

## Multiple accounts

```powershell
gdrivecopy accounts add personal --credentials "C:\Private\client.json"
gdrivecopy accounts add work --credentials "C:\Private\client.json"
gdrivecopy accounts list
gdrivecopy accounts use personal
gdrivecopy copy drive:root "E:\Work backup" --account work
```

A sole profile is selected automatically. With several profiles, choose
`--account` or explicitly set a default. Drive verifies the email before every
run. A saved job stays bound to the original Google user even if the default changes.

Application state and the selected OAuth client JSON are excluded from uploads.
Conventional `credentials.json`, `token.json`, and `sessions.json` files at the
source root are also excluded for compatibility with old setups. Source or
destination paths inside the private state directory are rejected.

## Copy syntax

```text
gdrivecopy copy SOURCE DESTINATION [FILE_PATTERN ...] [OPTIONS]
gdrivecopy upload LOCAL_DIR FOLDER_ID [OPTIONS]
gdrivecopy download FOLDER_ID LOCAL_DIR [OPTIONS]
```

Exactly one endpoint must be `drive:FOLDER_ID` or `drive:root`; upload/download
aliases also accept a bare folder ID. Folder contents are copied recursively
into the destination without adding the source folder's own name. Find a folder
ID in its Drive browser URL after `/folders/`. Existing content is never overwritten.

```powershell
gdrivecopy copy drive:FOLDER_ID "E:\Photos" "*.jpg" "*.png"
gdrivecopy copy "D:\Archive" drive:FOLDER_ID --exclude "cache/*" --dry-run
gdrivecopy copy drive:FOLDER_ID "E:\Backup" --transfers 8 --bwlimit 50M
gdrivecopy copy drive:FOLDER_ID "E:\Backup" --existing checksum
gdrivecopy copy drive:root "E:\Documents" --export-docs office
```

Quote patterns. Matching is case-sensitive using Python shell-style globs:
includes match relative path or basename; excludes match relative paths with `/`
separators. Unlike robocopy, `*.*` does not mean every file. No pattern means all files.

| Option | Default and purpose |
|---|---|
| `--transfers N` | 4 concurrent files |
| `--chunk-size SIZE` | 64M per worker; multiple of 256K |
| `--bwlimit RATE` | Unlimited; aggregate payload pacing, e.g. 50M |
| `--retries N` | 8 attempts per upload/download range; metadata has a separate bounded budget |
| `--existing size\|checksum` | `size`: fast comparison of pre-existing files |
| `--verification checksum\|size` | `checksum`: verify new binaries against Drive MD5 |
| `--export-docs office\|pdf` | Opt-in Docs/Sheets/Slides/Drawings conversion |
| `--exclude GLOB` | Repeatable exclusion |
| `--dry-run` | Save and classify a plan without copying payloads |
| `--state-dir PATH` | Per-user account and job storage |
| `--no-progress` / `--quiet` | Plain logs / final report only |

`K`, `M`, and `G` use powers of 1024; rates are bytes per second.
Use `copy --help` and `resume --help` for full details.

## What “copied” means

New binary copies default to Drive MD5 verification. Large uploads hash as they
stream. Downloads validate each HTTP range, sync partial data before saving its
offset, verify the complete checksum, recheck source metadata, and only then
publish the final filename. An existing local file is never replaced.

| Outcome | Meaning |
|---|---|
| `copied` | Transferred/recovered binary content matched Drive MD5 |
| `copied_unverified` | Explicit size-only verification; equality was not established |
| `skipped_size` | Existing same-size item; content was not checked |
| `skipped_verified` | Existing local bytes were hashed and matched Drive metadata |
| `exported` | Native document converted to a supported format, not a byte-identical backup |
| `conflict` / `unsupported` / `failed` | Not successfully copied; inspect the reason |

The fast existing-file default intentionally compares path and size only.
Use `--existing checksum` when same-size differences matter. It reads local
files without downloading existing remote payloads merely to hash them. MD5
detects ordinary corruption, not deliberate collisions or a compromised source.

Native documents are unsupported unless export is selected. `office` produces
DOCX, XLSX, PPTX, and SVG; `pdf` produces PDFs. Extensions are appended to Drive
names. Google exports have a **10 MB API limit**, no range resume, and no source
MD5. Local SHA-256 receipts support recovery but cannot prove conversion
fidelity. Shortcuts and other native types remain unsupported. See
[Google's download/export contract](https://developers.google.com/workspace/drive/api/guides/manage-downloads).

## Cancel, recover, and inspect

Press **Ctrl+C once**. Scheduling and retry waits stop; active requests finish
or time out before the report is saved. Abrupt termination is also recoverable.

```powershell
gdrivecopy jobs
gdrivecopy resume JOB_ID
gdrivecopy resume JOB_ID --transfers 2 --bwlimit 20M
gdrivecopy report JOB_ID
gdrivecopy report JOB_ID --json
gdrivecopy report JOB_ID --audit-output "E:\job-audit.jsonl"
```

Resume also executes a saved dry-run plan; use `resume --dry-run` to preview
again. Resume is explicit: no startup task is installed after a Windows reboot.

Default state is `%LOCALAPPDATA%\gdrivecopy` on Windows and
`${XDG_STATE_HOME:-~/.local/state}/gdrivecopy` on Linux/macOS. Pass a custom
`--state-dir` on subsequent commands. The printed resume command includes it.

Each job has `job.sqlite3`, `report.json`, `files.csv`, and rotating `run.log`
files. SQLite holds the manifest, checkpoints, and append-only audit. JSON
provides statistics, limitations and an error sample; CSV covers every file.
Audit export uses JSON Lines and refuses to overwrite an existing file.

Exit codes: **0** complete/successful dry-run, **1** incomplete/paused/error,
**2** argument parsing error, **130** cancellation. Complete can include size-only
skips; read the outcome breakdown.

## Large migrations and limits

SQLite manifests, batched scanning and bounded worker queues avoid keeping the
entire transfer queue in memory. Resume reuses the original source manifest and
refreshes remote metadata through Drive's change feed. Completed downloads reuse
size/mtime receipts. An incomplete verified file rereads its local prefix to
reconstruct its hash, without retransferring confirmed bytes. Start a **new job**
to include new files or rehash completed destinations with `--existing checksum`.

Keep state on a reliable local disk. Plan capacity for destination content,
partial files, metadata and audit. Parts occupy the eventual file's space, not
a second full copy. Avoid other writers: Drive listings are not snapshots.

Google's [current quotas and transfer limits](https://developers.google.com/workspace/drive/api/guides/limits)
apply. Quotas and billing rules vary by project and can change; the app does not
estimate charges. More workers cannot overcome account/API/storage/daily limits.
Blocking quota errors pause for inspection and explicit resume.

Empty directories, links/junctions, permissions, sharing, comments and history
are not copied. Unsafe/case-colliding local names are reported instead of renamed
or merged. Binary modification times are preserved; upload creation time is used
only where the source OS exposes a real birth time.

See [OPERATIONS.md](OPERATIONS.md), [DEVELOPMENT.md](DEVELOPMENT.md), and
[PRD.md](PRD.md). Offline tests cover recovery protocols; live Google transfers
and a full 30 TB migration still require integration validation.

## Upgrading from 0.1

`upload` now uses named accounts and durable jobs. The old interface remains as
**`legacy-upload`**, with `auth` for its token setup. Existing `sessions.json`
jobs must use `legacy-upload` with the original source/destination/state paths.
JSON sessions are not imported into SQLite. Use `accounts add` and `copy` for
new jobs. Legacy mode retains its original reports/resume behavior without the
new dashboard, downloads, or cross-process job locking.
