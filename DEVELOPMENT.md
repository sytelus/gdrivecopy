# Development guide

## Repository map

| Path | Responsibility |
|---|---|
| `gdrivecopy/cli.py` | Argument validation, logging setup, dependency wiring, report/exit behavior |
| `gdrivecopy/auth.py` | OAuth consent, refresh, and atomic token persistence |
| `gdrivecopy/scanner.py` | Deterministic local traversal, metadata capture, exclusions, and scan errors |
| `gdrivecopy/drive.py` | Drive metadata operations and raw resumable/multipart HTTP protocol |
| `gdrivecopy/session.py` | Validated, thread-safe, atomic resumable-session cache |
| `gdrivecopy/persistence.py` | Private temporary files and shared atomic replacement |
| `gdrivecopy/uploader.py` | Classification, concurrency, retry, resume, throttling, and verification |
| `gdrivecopy/report.py` | Human and JSON reports |
| `gdrivecopy/models.py` | Shared immutable records, configuration, and mutable run statistics |
| `tests/` | Offline unit and integration-style tests with mocked Google services |
| `PRD.md` | Current product scope, safety invariants, and technical contract |
| `.github/workflows/ci.yml` | Windows/Linux checks on Python 3.10 and 3.14 |

There is no checked-in application data. Runtime files (`credentials.json`,
`token.json`, `sessions.json`, logs, reports, coverage output, and build output)
are ignored by Git.

## Setup

Create an isolated Python 3.10+ environment, then install the package and
development tools:

```bash
python -m pip install -e ".[dev]"
```

Runtime dependency versions live only in `pyproject.toml`. `requirements.txt`
installs the project itself so it cannot drift into a second dependency list.
Pytest and Ruff configuration also live in `pyproject.toml`.

## Quality checks

Run the same focused gates used for repository review:

```bash
python -m ruff format --check .
python -m ruff check .
python -m pytest --cov=gdrivecopy --cov-report=term-missing
python -m compileall -q gdrivecopy tests
python -m pip wheel --no-deps --wheel-dir dist .
```

The test suite must remain offline. Mock `DriveClient`, the discovery service,
or `AuthorizedSession`; never use a real OAuth token in a test.

## Design constraints for changes

- Keep all source and Drive mutations explicit. The tool may only create Drive
  folders/files and trash a newly created, unverified upload.
- Treat duplicate names and uncertain server acknowledgements as data-integrity
  problems, not cases for best-effort guessing.
- Keep `UploadStats` mutations on the main thread. Workers return `_WorkerResult`.
- Keep `SessionCache` internally synchronized and replace-atomic. Do not imply
  that it is safe for multiple processes.
- Bind sessions to the absolute source path, actual Drive parent ID, size, and
  precise mtime. Legacy or changed identities must restart, not resume elsewhere.
- Never send credentials or bytes to an unchecked cached URL. Validate the
  supported Google upload origin/path and disable raw HTTP redirects.
- Retain generated folder IDs across retries: a name lookup can lag after a
  successful create with a lost response.
- Session persistence is best effort, but in-memory state stays authoritative
  for the running uploader. Disk errors must not bypass post-upload cleanup.
- Preserve bounded dispatch: never submit the entire source tree to the executor
  at once.
- Keep a separate `AuthorizedSession` per worker thread. Do not serialize file
  payload uploads through the discovery-client lock.
- A retry after any ambiguous resumable failure must query the saved session
  before sending more bytes.
- Do not immediately retry an ambiguous multipart request. It has no status URI,
  so the next program run must rescan Drive before deciding whether to resend.
- A local read failure must occur before creating a small-file Drive item where
  possible. If a completed item cannot be verified, clean it up before retry.
- Exact tool-owned runtime paths remain mandatory exclusions even if general
  include/exclude options are added later.
- Update `README.md`, `PRD.md`, report JSON tests, and CLI tests together when a
  user-visible field or behavior changes.

## Test organization

- `test_auth.py`: valid, corrupt, missing, refreshed, and atomically saved tokens
- `test_cli.py`: parsing, validation, configuration wiring, and exit statuses
- `test_scanner.py`: ordering, timestamps, exclusions, symlinks, and errors
- `test_drive.py`: listing, conflicts, folder idempotency, HTTP protocol, and errors
- `test_session.py`: validation, persistence, and thread safety
- `test_persistence.py`: failed replacement, temporary-link avoidance, and POSIX permissions
- `test_integrity_regressions.py`: identity isolation, path collisions, recovery
  cleanup, source replacement, authentication retry budget, and quota draining
- `test_uploader.py`: classification, retry, resume, verification, mutation,
  throttling, cancellation, folder creation, and run-level statistics
- `test_report.py`: formatting and atomic JSON schema persistence

When fixing a bug, first add a regression test that fails for the unsafe or
incorrect behavior. Prefer assertions about observable outcomes—Drive calls,
cache retention, report fields, and exit status—over internal call ordering.

## Manual integration checklist

Automated tests cannot prove live Google behavior. Before a release intended for
real migration work, use a disposable My Drive folder and non-sensitive files to
verify:

1. First-time OAuth and cached-token refresh.
2. Small, empty, and multi-chunk uploads.
3. Process interruption followed by byte-level resume.
4. Same-size skip and different-size conflict behavior.
5. Nested folder creation and an intentional duplicate-folder conflict.
6. MD5 verification and timestamps in Drive metadata.
7. Aggregate bandwidth limiting with multiple workers.
8. Report/log placement inside and outside the source tree.
9. Recovery of a folder create whose response was lost, using its generated ID.
10. Legacy session rejection and a restart using the new session identity fields.

CI adds the operating-system and Python-version matrix; a local Windows run
alone does not validate the Linux/POSIX permission behavior or Python 3.10.
Integration tests must also keep other writers away from the destination: the
initial Drive listing is not a transactional snapshot.

Never commit the live credentials, token, session URI, logs containing private
paths, or sample personal data. Record the Python version and Google client
library versions used for the integration pass.
