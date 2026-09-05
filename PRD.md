# Product and technical requirements: gdrivecopy

## 1. Purpose

`gdrivecopy` uploads a large local directory tree to a folder in personal Google
Drive without staging file content on the system disk. It is intended for
one-way migrations that need bounded memory, restartable large-file transfers,
clear conflict handling, and evidence that newly uploaded bytes arrived intact.

## 2. Scope

### Required behavior

- Upload local regular files and recreate their relative directory hierarchy.
- Stream content directly from source files; never build a second local content
  cache.
- Skip a Drive item only when its relative path and byte size match.
- Preserve conflicting existing Drive items and report path/size conflicts.
- Resume interrupted uploads larger than 8 MiB from a validated Drive session.
- Preserve modification time and a real creation/birth time when the platform
  exposes one. Never substitute Linux inode-change time for creation time.
- Verify files completed by the current run with Drive's returned MD5 checksum,
  unless the user explicitly disables verification.
- Never modify or delete source content.
- Never overwrite or trash a Drive item found during the initial destination
  scan. Only a new item tied to the current upload/session may be moved to trash
  when it cannot be verified.
- Account visibly for symlinks, excluded runtime files, scan failures, upload
  failures, conflicts, resumes, and blocking-quota stop conditions.
- Return a nonzero process status whenever the requested copy is incomplete or
  conflicted.

### Deliberate exclusions

- Download, bidirectional synchronization, mirroring, and deletion propagation
- Overwriting or reconciling same-path files with different sizes
- Downloading existing Drive files for checksum comparison
- Google Photos integration or conversion to Google Workspace editor formats
- Shared-drive-specific query and permission options
- Cross-process coordination of one `sessions.json` file
- Empty-directory preservation and filesystem/Drive snapshots
- Include/exclude globs, scheduling, and encryption

## 3. Safety invariants

1. **Drive is authoritative for completed paths.** A visible same-size item is
   skipped. A different-size item is a conflict, not an overwrite target.
2. **Ambiguity fails closed.** Duplicate Drive names, file/folder path
   collisions, names containing the local path separator `/`, malformed upload
   ranges, and invalid completion metadata must never be resolved arbitrarily.
3. **Detect ordinary source mutation.** Size, nanosecond modification time, and
   file identity are checked around transfer; changed or link-replaced files
   fail. An uploaded item from a detected race is trashed. These checks are not
   a filesystem snapshot or a security boundary against hostile concurrent writers.
4. **Retry only when safe.** Temporary resumable-upload and rate-limit failures
   are retried. An ambiguous one-request multipart result stops until the next
   run rescans Drive. Client errors are permanent for that run. If an
   unverified new item cannot be trashed, the file stops rather than risking a
   duplicate.
5. **Sensitive runtime artifacts do not become payload.** Configured OAuth
   credentials, token, session cache, active log, report, and atomic temporary
   files are excluded when they fall inside the source tree.
6. **State writes are replace-atomic.** Token, session, and JSON report files are
   written to an exclusively created private sibling temporary file and then
   replaced. Windows inherits directory ACLs. Overlapping runtime paths are
   rejected by the CLI before writes.

## 4. Platform and dependencies

- Python 3.10+
- Primary platform: Windows 10/11
- Best-effort support: macOS and Linux
- Google Drive API v3
- Runtime packages are defined once in `pyproject.toml`:
  `google-api-python-client`, `google-auth-oauthlib`, `google-auth`, `requests`,
  and `httplib2`
- Standard library concurrency, hashing, filesystem, logging, and JSON modules

## 5. End-to-end workflow

### Phase 1: destination scan

Validate the destination as an existing, untrashed My Drive folder, resolve its
actual ID (including the `root` alias), and walk its descendants with an explicit
stack to build:

- `relative_path -> DriveFile(id, name, size, md5_checksum)`, where size is
  unknown for Drive-native items that do not expose a byte size
- `relative_folder_path/ -> folder_id`

The scan follows Drive pagination and retries transient list failures up to five
total attempts with full-jitter exponential backoff. Because Drive names are not
unique, duplicate or ambiguous paths raise an explicit conflict instead of
overwriting an in-memory entry.
Incomplete searches, repeated pagination tokens, and invalid metadata abort
the scan. Empty pages with a continuation token must still be followed.

### Phase 2: local scan and classification

Walk the source tree in deterministic name order without following symlinks or
Windows directory junctions. For each regular file, capture the absolute path,
relative POSIX-style path, size, ISO modification time, nanosecond modification
time, device/file identity, and a real supported creation time. Windows cloud
placeholders are regular source files; only name-surrogate reparse points are
treated as links.

Filesystem traversal/stat errors are retained in the final report. Exact paths
owned by the running tool are excluded before inspection.

Classify each successfully scanned file:

| Destination state | Action |
|---|---|
| No item at relative path | Queue upload |
| Same path and size | Skip unchanged |
| Same path, different or unavailable size | Record conflict and skip |
| Folder occupies file path, or file occupies an ancestor path | Record path conflict and skip affected files |

Dry run ends after classification, logs every upload candidate, and does not
create Drive files or mutate cached sessions.

### Phase 3: bounded concurrent upload

Use a `ThreadPoolExecutor` with at most `transfers` outstanding futures, so work
queue memory does not scale with the source file count. Each worker owns a
separate `AuthorizedSession`; discovery-client operations are serialized because
their underlying transport is not thread-safe.

Before creating a missing parent folder, query all pages for existing same-name
children of any type. Multiple items or a file at the folder path are conflicts.
Reserve a Drive-generated ID for each new folder and retain it across retries
within the run; an HTTP 409 for that reserved ID means the earlier create
succeeded. This avoids depending on listing freshness after an ambiguous create.

- Files at or below 8 MiB use one multipart upload.
- Multipart reads are capped at 8 MiB plus one byte to detect growth without
  loading an unexpectedly large file into memory.
- A multipart transport or server failure is ambiguous because no status URI
  exists. Stop that file and let the next run reconcile Drive before retrying.
- Larger files use Drive's resumable protocol and the configured chunk size.
- Non-final chunks must be multiples of 256 KiB.
- Every raw HTTP operation has a 10-second connection timeout and a 300-second
  response timeout, with redirects disabled. The uploader owns the single
  explicit 401 refresh retry for file uploads.
- `--bwlimit` coordinates one process-wide payload schedule across workers; it
  is an average payload limit, not a packet-level traffic shaper.

### Phase 4: verification and reporting

For a new upload, require Drive completion metadata with an item ID. When MD5
verification is enabled, require a returned checksum and compare it with the
local stream. Missing/mismatched checksums and source mutations cause the new
item to be moved to Drive trash before retry or failure.

The human-readable report is printed to stdout. `report.json` is written to the
log directory with the following fields:

- `files_scanned`, `files_excluded`, `scan_errors`, `symlinks_skipped`
- `files_to_upload`, `files_uploaded`, `bytes_uploaded`, `files_resumed`
- `files_skipped`, `size_mismatches`, `path_conflicts`, `files_failed`
- `duration_seconds`, `quota_limit_hits`
- `errors`, `mismatch_details`

`bytes_uploaded` is confirmed payload for successful completions during this
run. It excludes the already-confirmed prefix of a resumed upload, protocol
overhead, and retransmission bytes from failed attempts.

## 6. Resumable session cache

`sessions.json` maps a relative source path to:

```json
{
  "relative/path/video.mp4": {
    "session_uri": "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=example",
    "file_size": 53687091200,
    "mtime": "2026-06-15T10:30:00+00:00",
    "mtime_ns": 1781519400000000000,
    "source_path": "D:\\Photos\\relative\\path\\video.mp4",
    "parent_id": "actual-drive-parent-id"
  }
}
```

The cache is optional performance state, not correctness state. Its JSON shape,
Google upload HTTPS origin/path, nonnegative size, and identity fields are
validated on load. Corrupt files are ignored as a whole.

A cached session is usable only when absolute source path, actual Drive parent
ID, size, ISO mtime, and nanosecond mtime match and Drive confirms its state.
Legacy entries without identity fields are discarded; an interrupted transfer
may restart once after upgrading. A `400`/`404`/`410` status query or
transfer response discards the rejected session. Other errors, including rate
limits and temporary server failures, preserve the session and flow through
normal retry handling. A completed-session response must include file metadata
and is checksum-verified before success is recorded.

The cache is protected by a reentrant lock for worker threads and is saved after
every actual mutation. A persistence failure logs a warning but preserves live
in-memory state, allowing verification and cleanup to finish. Resume after a
process restart may be lost. Simultaneous processes must use different cache files.

## 7. Failure handling

| Failure | Required action |
|---|---|
| `429`, `403 rateLimitExceeded` | Full-jitter exponential backoff and retry |
| `408`, `500`, `502`, `503`, `504` or transport failure during resumable upload/initiation | Retry after querying a saved session where applicable |
| The same failure after a multipart request is sent | Stop that file; rescan Drive on the next run before retrying |
| `403 dailyLimitExceeded` API quota | Signal all workers, cancel queued work, drain running work, report once |
| `401` | Refresh credentials once for the file, then fail permanently |
| Other `4xx` | Fail the file permanently for the run |
| Rejected resumable URI (`400`/`404`/`410`) | Remove URI and start a new session |
| Malformed/partial chunk acknowledgement | Re-query the session on retry; never assume bytes were accepted |
| Local read/stat or source mutation | Fail permanently for the run |
| Missing/mismatched MD5 | Trash new item and retry |
| Failure to trash an unverified item | Stop that file without retry to avoid a duplicate |

File upload attempts are capped at eight per run. A circuit breaker pauses for
60 seconds after ten consecutive nonpermanent file failures. Metadata listing
has its own bounded five-attempt policy because it runs outside the file worker
retry loop.

## 8. CLI contract

```text
gdrivecopy [--version]
gdrivecopy auth [--credentials PATH] [--token PATH]
gdrivecopy upload <source_dir> <drive_folder_id> [options]

  --transfers N
  --chunk-size SIZE
  --bwlimit RATE
  --dry-run
  --log-dir PATH
  --log-level {DEBUG,INFO,WARNING,ERROR}
  --credentials PATH
  --token PATH
  --sessions PATH
  --no-checksum-verify
  --quiet
```

Numeric sizes accept raw bytes or binary `K`, `M`, and `G` suffixes. Transfers
and bandwidth must be positive. Chunk size must be at least 256 KiB and aligned
to 256 KiB.

Exit status is 1 for a missing command, upload failure, scan error, size/path
conflict, required runtime-file write failure, or blocking-quota interruption.
Argument errors use argparse's status
2.

## 9. External limits

Limits are external policy, not hard-coded scheduling assumptions. As of
September 2026, Google documents:

- 750 GB uploaded/copied daily for Google Workspace users
- 5 TB maximum uploaded file size
- One-week resumable-session lifetime
- 256 KiB alignment for non-final upload chunks
- 500,000 items per My Drive folder, except the My Drive root

Drive API quota accounting changed in May 2026 and can differ for projects that
predate the change. Operators must consult the current Cloud Console and
[official Drive API usage limits](https://developers.google.com/workspace/drive/api/guides/limits).

## 10. Quality gates

The repository must pass:

```bash
python -m ruff format --check .
python -m ruff check .
python -m pytest --cov=gdrivecopy --cov-report=term-missing
python -m compileall -q gdrivecopy tests
```

Tests must not contact Google Drive. OAuth, discovery requests, and raw HTTP
responses are mocked. High-risk regression coverage includes session expiry and
temporary failures, byte-range validation, checksum cleanup, source mutation,
Drive path ambiguity, directory symlinks, runtime-file exclusion, CLI exit
status, authentication cache recovery, bounded retry counts, and concurrent
session-cache writes.

## 11. Future work

- Graceful Ctrl+C coordination with an explicit user-cancel event
- Optional include/exclude glob rules distinct from mandatory runtime-file safety
- Progress display that does not compromise quiet/log-only operation
- Shared-drive support with explicit corpora and permission semantics
- Optional verification mode for pre-existing Drive files
