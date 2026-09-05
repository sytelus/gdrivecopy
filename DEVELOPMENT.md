# Development guide

## Architecture

| Module | Responsibility |
|---|---|
| `cli.py`, `commands.py` | Help/parsing, account wiring, locks, signals, logging, exit codes |
| `accounts.py`, `auth.py` | Profiles, server identity, OAuth and private token persistence |
| `jobstore.py` | SQLite schema, manifests, checkpoints, audit, OS locks |
| `inventory.py` | Persistent Drive tree, cursor recovery, change feed, folder adapter |
| `transfer.py` | Fixed manifest, namespace preflight, reconciliation, dispatch, reports |
| `uploader.py` | Multipart/resumable transfers, source checks, retry, checksum/cleanup |
| `downloader.py` | Safe paths, durable ranges, checksum, export, no-clobber publication |
| `drive.py` | Validated metadata operations and raw HTTP protocol |
| `control.py`, `terminal.py` | Cancellation, bounded progress model, Rich dashboard/report |
| `scanner.py` | Deterministic traversal, metadata, exclusions and errors |
| `persistence.py` | Unique private temporary files, sync and atomic replacement |
| `session.py`, `report.py`, `models.py` | Shared records and compatibility JSON state/reporting |

There is no personal data checked in. `tests/fake_drive.py` is synthetic offline
test data. Runtime state/build outputs are ignored. Dependencies and tool
settings live in `pyproject.toml`; `requirements.txt` installs the package itself.

## Setup and validation

```powershell
python -m pip install -e ".[dev]"
python -m ruff format --check .
python -m ruff check .
python -m pytest --cov=gdrivecopy --cov-report=term-missing
python -m compileall -q gdrivecopy tests
python -m pip wheel --no-deps --wheel-dir dist .
```

CI in `.github/workflows/ci.yml` covers Windows/Linux on Python 3.10/3.14. Keep
tests offline: mock OAuth, discovery and raw HTTP. A local Windows run alone
does not validate every supported platform/interpreter.

## Building and releasing

Use an isolated Python environment on the target OS. PyInstaller produces a
native executable plus support files; it is not a cross-compiler.

```sh
python -m pip install ".[build]"
python -m build
python scripts/build_binary.py
python scripts/release_checksums.py dist
```

The script builds into `dist/gdrivecopy`, runs help/version/offline diagnostics
from outside the checkout, and archives the entire bundle. It includes the Drive
discovery data, TLS certificates, runtime dependency licenses and exact build
versions. It fetches the matching CPython license from the official CPython repo.
Build files and generated specs stay under ignored `build/` and `dist/` paths.

`release.yml` builds Windows x64, Linux x64 and macOS ARM64 on main and version
tags, tests each platform, and checks wheel/sdist metadata. Main builds are
downloadable Actions artifacts. To release:

1. Update package version, changelog and `.github/RELEASE_NOTES.md`.
2. Push reviewed changes to main and wait for quality/native-package checks.
3. Tag that tested commit `vVERSION` and push the tag. Do not move published tags.
4. The tag workflow rebuilds, checks the version, adds SHA-256 hashes, and
   publishes all artifacts together. Download assets and verify hashes/launch.

No PyPI upload, signing certificate or notarization is configured. Native builds
are not byte-for-byte reproducible; `BUILD_INFO.json` records dependency versions
and source commit for investigation. Offline tests do not replace live migration
validation. Publishing requires repository Actions with contents-write permission
only in the final publish job.

## State ordering

Each job has a SQLite WAL database with `synchronous=FULL`, schema version 1,
and a process lock. Threads share a serialized connection. Transactions never
span payload requests. Use a local filesystem with reliable sync/lock semantics.

`meta` stores config/run summaries; `remote`, `folders`, `folder_seen` and
`page_tokens` hold inventory; `files` contains the source plan, status, reserved
ID, session, offset and receipt; `events` is append-only audit. Namespace tables
detect file/directory aliases. Reports can open state read-only while a job runs.

Critical orderings:

1. Persist a generated create ID **before** sending payload; retry that ID after
   a lost response. Persist folder IDs across restarts too.
2. Write/fsync a download range **before** saving its offset. Truncate excess
   bytes on resume, restart missing/short parts, rehash a verified prefix locally.
3. Save the verification receipt **before** final publication. Windows rename
   and POSIX hard-link publication refuse existing targets. Recover a gap
   between publication and status by verifying local content/receipt.
4. Recheck upload source identity before accepting completion. Trash a newly
   created unverified item before trying another ID; failed cleanup stops.
5. Capture a change cursor **before** the initial scan; commit cursor advance
   together with affected-folder invalidation.

Modern checkpoint/identity persistence errors stop the operation. Legacy JSON
sessions retain best-effort persistence; do not extend that fallback to SQLite.

## Concurrency and complexity

Manifest batches are 500 rows; Drive pages are at most 1,000 items. Pending files
use indexed keyset pagination and at most `transfers` futures. Never submit or
display the whole manifest. Local scanning uses callbacks; `os.walk` still
allocates one directory's entries and scan errors are retained for the run.

Resume streams completed records, reads the database and stats completed local
destinations. It is not constant-time in file count. Change feeds can be large
on busy accounts. Payload memory includes worker chunks and temporary HTTP
buffers: `workers × chunk_size` is not an exact memory ceiling.

Small uploads can reread up to 8 MiB for hashing/multipart construction; large
uploads hash while streaming. Validate cached upload URLs and disable raw HTTP
redirects. Keep worker payload sessions separate from the metadata-client lock.

## Tests and release gate

Existing suites cover upload protocol, source identity, cleanup, legacy state,
auth, scanning and reports. `test_jobs.py` covers durable jobs/crash boundaries;
`test_download_protocol.py` validates responses independently of the fake server;
`test_commands.py` covers profiles, CLI wiring and dashboard rendering. Assert
observable safety properties rather than reproducing implementation details.

Update README, operations guidance, PRD, CLI help and report consumers together.
Fail closed on newer state schemas. Do not commit credentials, capabilities,
private paths or real account identifiers in fixtures/screenshots.

Before a real migration release, independently compare hashes in a disposable
My Drive folder. Test OAuth/refresh, two accounts, empty/small/large transfers,
exports, timestamps, conflicts, Ctrl+C, forced termination/reboot, pacing, quotas
and reports. Record platform/dependency versions and measured rates. Offline
tests cannot establish live Google behavior, all terminal renderers or sustained
multi-terabyte throughput.
