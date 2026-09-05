# gdrivecopy

`gdrivecopy` is an upload-only command-line utility for copying large local
directory trees into a folder in Google Drive. It streams file content directly
from the source filesystem, recreates the folder hierarchy, resumes interrupted
large uploads, and verifies newly uploaded bytes with Drive's MD5 checksum.

The project is designed for migrations where local staging would consume too
much disk space. It does not delete or modify source files, overwrite existing
Drive files, or synchronize changes back to the computer.

## Behavior at a glance

- Files already present at the same relative path and size are skipped without
  being downloaded or rehashed.
- A path that exists on Drive with a different or unavailable byte size is
  reported as a conflict and left unchanged. The command exits nonzero so it
  cannot look fully successful.
- File-versus-folder collisions, including a Drive file blocking a local parent
  directory, are reported as path conflicts before uploads are scheduled.
- Files larger than 8 MiB use resumable uploads; smaller files use one
  multipart request.
- Creation time is preserved on Windows and on platforms that expose a real
  birth time. Modification time and original file bytes are preserved on every
  supported platform. Linux inode-change time is not misrepresented as file
  creation time.
- File and directory symlinks, plus Windows directory junctions, are reported
  and skipped. Unreadable or unsupported filesystem entries are reported as
  scan errors. Ordinary Windows cloud placeholders are not mistaken for links;
  reading them may cause the filesystem provider to download their content.
- Duplicate or path-ambiguous item names on Drive stop the affected operation
  instead of allowing an arbitrary folder or file to win.
- OAuth credentials, token, session cache, active log, report, and their
  temporary files are excluded automatically if they are inside the source
  tree.

Integrity verification applies to files completed by the current run. Existing
Drive files are intentionally compared by path and size only; `gdrivecopy` does
not spend bandwidth downloading them for verification.

## Requirements

- Python 3.10 or newer
- A Google account with enough Drive storage
- A Google Cloud project with the Google Drive API enabled

This version targets a folder in personal **My Drive**. Shared-drive-specific
options are not implemented, and shared-drive destinations are rejected.
Empty local directories are not copied; folders are created for file parents.

## Google Cloud and OAuth setup

Google changes the Cloud Console navigation periodically. As of September 2026,
the relevant pages are under **Google Auth Platform**; older projects may still
show **APIs & Services → OAuth consent screen**.

1. Open the [Google Cloud Console](https://console.cloud.google.com/), create or
   select a project, and enable the **Google Drive API** from **APIs & Services →
   Library**.
2. Open **Google Auth Platform** and configure **Branding** and **Audience**. For
   a personal Google account, choose an external audience and add the account as
   a test user if the app remains in testing mode.
3. Open **Clients**, create an OAuth client with application type **Desktop
   app**, and download its JSON file.
4. Save that file as `credentials.json` in the directory where you will run the
   command, or pass its location with `--credentials`.

The app requests the full Drive scope because it must list files created by
other clients. Treat both `credentials.json` and especially `token.json` as
secrets. Do not commit, share, or place them in an untrusted directory.

For OAuth apps left in Google's **Testing** publishing state, test-user
authorizations and refresh tokens can expire after seven days. For a migration
that will run longer, review the current Audience/publishing requirements in the
[Google OAuth token-expiration documentation](https://developers.google.com/identity/protocols/oauth2#expiration).

## Installation

```bash
git clone https://github.com/sytelus/gdrivecopy.git
cd gdrivecopy
python -m pip install .
```

For an editable development installation with test and lint tools:

```bash
python -m pip install -e ".[dev]"
```

## Authentication

Run the consent flow once:

```bash
gdrivecopy auth --credentials credentials.json --token token.json
```

Your browser opens for sign-in and consent. An unverified-app warning can be
normal for a private OAuth project; confirm the project and requested access
before proceeding. A valid token is refreshed automatically and is saved with
restricted file permissions where the operating system supports them.
Revoked or expired refresh grants trigger fresh consent. Network and OAuth
client-configuration errors remain visible failures.

## Finding the destination folder ID

Open the destination folder in Google Drive. Its URL resembles:

```text
https://drive.google.com/drive/folders/1aBcDeFgHiJkLmNoPqRsTuVwXyZ
```

The text after `/folders/` is the folder ID.

## Usage

Upload a directory tree:

```bash
gdrivecopy upload "D:\Photos" 1aBcDeFgHiJkLmNoPqRsTuVwXyZ
```

Preview the scan without creating Drive files or changing session entries:

```bash
gdrivecopy upload "D:\Photos" 1aBcDeFgHiJkLmNoPqRsTuVwXyZ --dry-run
```

The dry-run report includes `Files to upload`, and each candidate is written to
the log.

Limit aggregate payload scheduling across all workers to approximately 50 MiB/s:

```bash
gdrivecopy upload "D:\Photos" 1aBcDeFgHiJkLmNoPqRsTuVwXyZ --bwlimit 50M
```

Use more concurrent uploads:

```bash
gdrivecopy upload "D:\Photos" 1aBcDeFgHiJkLmNoPqRsTuVwXyZ --transfers 8
```

Place runtime state outside the source tree:

```bash
gdrivecopy upload "D:\Photos" 1aBcDeFgHiJkLmNoPqRsTuVwXyZ \
  --credentials "D:\gdrivecopy-state\credentials.json" \
  --token "D:\gdrivecopy-state\token.json" \
  --sessions "D:\gdrivecopy-state\sessions.json" \
  --log-dir "D:\gdrivecopy-state\logs"
```

On PowerShell, use a backtick rather than `\` for line continuation, or put the
command on one line.

### Options

```text
gdrivecopy upload <source_dir> <drive_folder_id> [options]

  --transfers N          Concurrent uploads; N must be at least 1 (default: 4)
  --chunk-size SIZE      Resumable chunk size, a multiple of 256K (default: 64M)
  --bwlimit RATE         Aggregate payload rate such as 50M (default: unlimited)
  --dry-run              Scan and log candidates without uploading
  --log-dir PATH         Log and report directory (default: current directory)
  --log-level LEVEL      DEBUG, INFO, WARNING, or ERROR (default: INFO)
  --credentials PATH     OAuth client JSON (default: ./credentials.json)
  --token PATH           OAuth token cache (default: ./token.json)
  --sessions PATH        Resumable-session cache (default: ./sessions.json)
  --no-checksum-verify   Disable post-upload MD5 verification
  --quiet                Suppress console logging; still print the final report
```

Sizes accept bytes or a binary `K`, `M`, or `G` suffix. Chunk size must be at
least 256 KiB. `--no-checksum-verify` weakens the main integrity guarantee and
should be used only when the tradeoff is understood.

## Workflow and safety guarantees

1. **Scan Drive.** The target tree is listed recursively into path maps. Because
   Drive permits duplicate names, a duplicate or otherwise ambiguous path is a
   hard error rather than a nondeterministic match. The destination is validated
   as an existing, untrashed My Drive folder before listing starts; incomplete
   search results also stop the run.
2. **Scan local files.** Regular files are recorded with size, nanosecond mtime,
   and supported timestamps. Symlinks and scan errors are accounted for. The
   tool's own sensitive/runtime files are excluded.
3. **Classify.** Same-path/same-size files are skipped. Same-path/different-size
   files are reported and preserved. Missing paths are upload candidates.
4. **Upload.** Parent folders are looked up before creation, and each new folder
   uses a generated Drive ID that is retained across retries within the run.
   This prevents duplicate retry creations even if listing has not caught up
   with a lost response. Worker HTTP
   sessions are isolated, requests have finite timeouts, and safely retryable
   errors use bounded exponential backoff. If a one-request small-file upload
   has an ambiguous network or server result, that file stops until the next
   run can rescan Drive; this prevents an immediate retry from creating a
   duplicate.
5. **Verify.** The local file is checked for changes during transfer. Drive's
   returned MD5 must match when verification is enabled. A newly created but
   unverified item is moved to Drive trash before retrying. Existing Drive items
   found by the initial scan are never trashed by this process.
6. **Report.** A human-readable report is printed and `report.json` is written
   atomically in the log directory.

The command exits with status 1 when uploads fail, the local scan is incomplete,
a size or path conflict exists, a runtime file cannot be written (except the
optional session cache), or a blocking Drive quota stops the run. Symlinks and
intentional tool-file exclusions are reported but do not make the run fail.

`bytes_uploaded` counts payload bytes confirmed for successfully completed files
in this run. It excludes the already-confirmed prefix of a resumed file and does
not attempt to estimate protocol overhead or bytes retransmitted during failed
attempts.

## Session cache

`sessions.json` maps an in-progress large file to its resumable session URI,
absolute source path, actual Drive parent ID, scanned size, and precise
modification time. It enables byte-level resume after a
process or network interruption. Entries are validated before use; a changed
source file or a rejected `400`/`404`/`410` session is discarded, while
temporary status-query failures keep the entry for retry.

Older cache entries without source/destination identity are discarded, so an
interrupted upload may restart once after upgrading. Sessions are never reused
for another source path or Drive parent. Only HTTPS sessions at the supported
Google Drive upload endpoint are accepted, and upload requests do not follow
redirects.

The cache is a disposable optimization, not the source of truth. Deleting it
causes incomplete files to restart from byte zero. Do not share one session file
between simultaneously running `gdrivecopy` processes; thread safety is
provided within one process, not across processes.
If the cache cannot be saved, a warning is logged and live in-memory resume
continues. Restart recovery may be unavailable until the filesystem issue is
resolved. Token, session, and report writes use private, uniquely named sibling
temporary files; Windows permissions depend on the containing directory's ACLs.
Use trusted state directories and distinct paths for each runtime file.

Keep the source and destination stable during a run. Size, precise mtime, and
file identity checks catch ordinary source mutations; the utility does not take
a filesystem snapshot or coordinate with other Drive writers. Completed files
are still compared by path and size when the next run starts. A fatal error
before reporting leaves any previous `report.json` unchanged; check the current
log and process exit status. Scanned metadata remains in memory proportional to
the number of files; only payload buffering and queued uploads are bounded by
the chunk size and worker count.

## Current Google Drive limits

Google currently documents a 750 GB daily upload/copy allowance for Workspace
users and a 5 TB maximum uploaded file size. Resumable session URIs
expire after one week, and non-final chunks must be multiples of 256 KiB. My
Drive folders can contain up to 500,000 items, with an exception for the My Drive
root.

Google changed Drive API quota accounting in May 2026, and quotas can differ for
older and newer Cloud projects. Check the live Cloud Console and Google's
[Drive API usage limits](https://developers.google.com/workspace/drive/api/guides/limits)
rather than relying on a fixed request-per-minute number in this repository.
See also Google's current [upload protocol](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
and [storage/upload limits](https://support.google.com/a/answer/172541).

## Troubleshooting

### OAuth credentials not found

Check `--credentials` and confirm that the downloaded desktop-client JSON exists.
Current projects expose OAuth clients under **Google Auth Platform → Clients**.

### Token expired or revoked

Run the `auth` command again. A corrupt token cache is ignored automatically and
replaced through the consent flow when valid client credentials are available.
If the OAuth app is in Testing, remember the seven-day authorization behavior
described above.

### Daily upload or API limit reached

The run exits status 1. Drive's documented `dailyLimitExceeded` reason refers to
a project API quota or owner-configured cap; review the Cloud Console quota
settings. Google's separate 750 GB upload/copy allowance refreshes within 24
hours. After the applicable limit is resolved, completed same-size paths are
skipped and partial large files resume when their sessions are still valid.

### Size mismatch

The local and Drive items share a relative path but have different sizes.
`gdrivecopy` leaves both untouched, records the exact sizes, and exits status 1.
Resolve the conflict manually, then run again.

### File/folder conflict

A local file occupies the path of a Drive folder, or a local parent directory
occupies the path of a Drive file. Affected files are skipped, `path_conflicts`
is incremented, and the command exits status 1. Resolve the naming conflict
before retrying; unrelated paths can still upload.

### Duplicate or ambiguous Drive path

Google Drive allows duplicate names in one folder and names containing `/`, but
a local directory tree cannot represent those cases unambiguously. Rename or
remove the ambiguity in the destination and retry.

### Source file changed during upload

The file was written or replaced after the scan began. The newly created Drive
item is moved to trash if necessary, the file is marked failed, and the next run
will scan its stable metadata again.

### Small-file upload has an ambiguous result

A timeout or server error may arrive after Drive accepted a one-request
multipart upload. `gdrivecopy` does not immediately retry that file because it
cannot query a multipart request's status and a retry could create a duplicate.
Run the command again: its initial Drive scan will skip a completed same-size
item or safely retry the file if none exists.

### Upload seems slow

Inspect the log for retry or rate-limit messages. More workers help primarily
with many files; they do not bypass account, API, network, or per-file limits.
Large chunk sizes also multiply memory use by the worker count.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for architecture, invariants, commands, and
the testing strategy.

## License

MIT
