# PRD: gdrivecopy

## 1. Goal

Create a reliable, robust, and fast command-line utility to upload very large amounts of files to Google Drive. The utility must:

- **Conserve bandwidth and disk space** -- no local staging or caching; stream directly from source.
- **Be resilient to failures** -- handle network and API errors, survive crashes, resume on re-run.
- **Skip already-uploaded files** without touching them or updating their metadata.
- **Preserve file metadata** after upload: creation date, modification date, EXIF data.
- **Never lose data** -- never delete or overwrite source files; never modify successfully uploaded files.
- **Be stateless** -- no local database. Drive itself is the source of truth. The tool can run from any machine.
- **Log everything and produce a report** with statistics.

---

## 2. Why Build This?

**Google Drive for Desktop fails at scale.** It stages all files through a cache on C:\ regardless of source location. With large datasets (hundreds of GB, hundreds of thousands of files), the cache fills the system drive, Windows becomes unstable, and the sync silently stalls. Uploads above ~500 GB are impractical.

**rclone is close but not enough.** It cannot preserve `createdTime` (GitHub #4579, open since 2020) and cannot resume a partially-uploaded file after process restart -- a 50 GB file interrupted at 49 GB restarts from zero.

**The Google Drive API v3 supports everything we need**: byte-level resumable uploads, writable `createdTime`/`modifiedTime`, MD5 checksums for verification, and file listings for stateless skip detection.

---

## 3. Decisions

| Decision | Choice |
|----------|--------|
| Language | Python 3.10+ (network-bound; Google's Python SDK is the most mature) |
| Auth | OAuth 2.0 (personal Google One Ultra 30 TB account) |
| Target | Personal Google Drive (My Drive) only |
| Platform | Windows 10/11 primary; Linux/macOS nice-to-have |
| Direction | Upload only. Never modify or delete successfully uploaded files on Drive. |
| State | Stateless. Query Drive to determine what's already uploaded. No local database. |
| Symlinks | Log and skip (no Drive equivalent) |
| Photos | Rely on Google's auto-detection. No Photos API. |
| Filtering / scheduling / encryption | No |

---

## 4. Technical Design

### 4.1 Dependencies

- `google-api-python-client`, `google-auth-oauthlib`, `google-auth` -- Drive API v3, OAuth, and ``AuthorizedSession`` for chunked uploads
- stdlib: `hashlib`, `concurrent.futures`, `pathlib`, `logging`, `json`

### 4.2 Workflow

**Phase 1 -- Scan Drive:**

List all files and folders in the target Drive folder (recursive). Build an in-memory map of `relative_path → (id, size, md5Checksum)`. This is the source of truth for what's already uploaded.

Cost: ~1 API call per 1000 files. 500K files ≈ 500 calls ≈ 20-30 seconds.

**Phase 2 -- Scan Local & Upload:**

Walk the source directory recursively. For each local file:

1. **Skip symlinks** (log a warning).
2. **Check Drive map** by relative path:
   - Exists with same size → **skip**.
   - Exists with different size → **skip**, log WARNING with both sizes (size mismatch).
   - Not found → **upload** (step 3).
3. **Upload**:
   a. Ensure parent folder exists on Drive (create if missing, cache folder IDs in memory).
   b. Check `sessions.json` for a resumable session (see 4.3). If valid, resume. Otherwise, initiate a new upload.
   c. Stream chunks directly from disk.
   d. On completion, compare local MD5 to `md5Checksum` from the response. Mismatch → trash corrupted file, re-upload.

MD5 is only computed for files being uploaded, not for skipped files.

**Phase 3 -- Report:** print summary of uploaded, skipped, and failed files.

### 4.3 Resumable Upload Protocol

**Files > 8 MiB** use the Google Drive resumable upload protocol:

1. **Initiate**: `POST /upload/drive/v3/files?uploadType=resumable` with metadata (`name`, `createdTime`, `modifiedTime`, `parents`) and `fields=id,md5Checksum`. Returns a session URI.
2. **Stream chunks**: `PUT {session_uri}` with `Content-Range: bytes {start}-{end}/{total}`. Chunks must be multiples of 256 KiB (default 64 MiB).

**Files ≤ 8 MiB** use multipart upload (single request with metadata + content).

#### Session Cache (`sessions.json`)

An optional local JSON file that enables byte-level resume across restarts. It maps each in-progress file to its session URI, file size, and mtime at the time the upload started:

```json
{
  "relative/path/video.mp4": {
    "session_uri": "https://www.googleapis.com/upload/...",
    "file_size": 53687091200,
    "mtime": "2024-06-15T10:30:00"
  }
}
```

**This file is a disposable optimization, not state.** The tool works correctly without it -- files not on Drive are simply uploaded from scratch.

When a file needs uploading, check the cache. A session is **valid** only if ALL of these hold:
- Local file size and mtime match the cached values (file hasn't changed)
- Server responds 308 with a `Range` header (session still alive, returns confirmed byte offset)

If valid: resume from the confirmed offset. Otherwise: discard the entry, log the reason (expired / file changed / completed elsewhere), and upload from scratch. On completion or failure, update the cache accordingly.

### 4.4 Metadata Preservation

| Local Metadata | Drive API Field | How |
|----------------|-----------------|-----|
| Creation time (`st_ctime` on Windows) | `createdTime` | Set on `files.create` |
| Modification time (`st_mtime`) | `modifiedTime` | Set on `files.create` |
| File name | `name` | Set on `files.create` |
| EXIF / video metadata | Preserved in raw bytes | Drive auto-extracts to read-only `imageMediaMetadata` / `videoMediaMetadata` |
| Directory structure | Folder hierarchy | Recreated via API, folder IDs cached in memory |

### 4.5 Error Handling

| Error | Action |
|-------|--------|
| `429`, `403 rateLimitExceeded` | Backoff and retry |
| `403 dailyLimitExceeded` | Stop all uploads. Report resume time. Do not retry. |
| `500`, `502`, `503`, `504` | Backoff and retry |
| `401 Unauthorized` | Refresh OAuth token, retry once |
| `400`, non-rate-limit `403` | Permanent failure. Log and skip. |
| Connection timeout / reset | Retry the current chunk |
| Local file read error | Permanent failure. Log and skip. |
| MD5 mismatch after upload | Trash corrupted file, re-upload |

- **Max 8 retries per file per run.** Failed files are retried on the next run automatically (they won't be in the Drive map, so they'll be re-uploaded).
- **Circuit breaker**: 10 consecutive failures across all workers → pause 60 seconds.
- **Single retry layer**: disable retries in the HTTP client library to prevent amplification.
- **Backoff formula**: `delay = random(0, min(60, 2^attempt))`

### 4.6 Concurrency

```
Main Process
  ├── Drive Scanner (lists target folder, builds in-memory map)
  ├── Upload Pool (ThreadPoolExecutor, N concurrent uploads, default 4)
  └── MD5 computed per-file during upload (streaming, 8 KiB blocks)
```

Memory budget: `transfers × chunk_size` + Drive file map (~100 bytes per file, 500K files ≈ 50 MB). Default: 4 × 64 MiB + map ≈ 300 MiB.

---

## 5. CLI Interface

```
gdrivecopy upload <source_dir> <drive_folder_id> [options]

  --transfers N          Concurrent uploads (default: 4)
  --chunk-size SIZE      Chunk size (default: 64M, must be multiple of 256K)
  --bwlimit RATE         Max upload speed (e.g., 50M)
  --dry-run              Scan only, report what would be uploaded
  --log-dir PATH         Log directory (default: ./)
  --log-level LEVEL      Log verbosity (default: INFO)
  --credentials PATH     OAuth credentials JSON (default: ./credentials.json)
  --token PATH           OAuth token cache (default: ./token.json)
  --sessions PATH        Session cache file (default: ./sessions.json)
  --no-checksum-verify   Skip post-upload MD5 verification
  --quiet                Minimal output

gdrivecopy auth [--credentials]  Run OAuth flow and cache token
```

---

## 6. Logging and Reporting

**Log file**: `gdrivecopy_{timestamp}.log` (configurable via `--log-dir`).

- `INFO`: file uploaded/skipped/resumed, phase transitions, daily limit reached
- `WARNING`: size mismatch, symlink skipped, retry attempt, stale session discarded
- `ERROR`: permanent failure, integrity mismatch, auth failure
- `DEBUG`: API request/response, chunk progress

**Summary report** (stdout + `report.json`):
```
Files scanned:      234,567
Files uploaded:     231,234 (1.82 TB)
Files resumed:           3 (byte-level resume from session cache)
Files skipped:       3,100 (already on Drive)
Size mismatches:         8 (on Drive with different size -- see log)
Files failed:          121
Duration:        4h 23m 17s
Daily limit hit: 3 times
```

---

## 7. Google Drive Limits (Reference)

| Limit | Value |
|-------|-------|
| Daily upload cap | 750 GB / user / ~24h rolling window |
| API queries | 12,000 / 60 seconds |
| Per-file speed | ~40 MB/s (Google-side) |
| Resumable session lifetime | 1 week |
| Chunk alignment | Multiple of 256 KiB |
| Max file size | 5 TB |
| Items per user / folder | 500,000 recommended |
| Folder nesting | 100 levels |
| File name length | 255 characters |

30 TB at 750 GB/day ≈ 40 days.

---

## 8. Implementation Phases

### Phase 1: MVP
- OAuth 2.0 auth flow
- Drive folder listing (recursive, builds in-memory map, with retry on transient errors)
- Filesystem scanner with skip detection (path + size, size mismatch warnings)
- Folder creation on Drive
- Single-file resumable upload with optional session cache (`sessions.json`) for byte-level resume
- Metadata preservation (`createdTime`, `modifiedTime`)
- Post-upload MD5 verification
- Error handling with retries, circuit breaker
- `--dry-run` mode
- Logging and summary report

### Phase 2: Performance
- Concurrent uploads (bounded `ThreadPoolExecutor`)
- Progress display
- `--bwlimit`

### Phase 3: Polish
- `--exclude` / `--include` glob patterns
- Graceful shutdown on Ctrl+C
