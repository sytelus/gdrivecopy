# Changelog

## Unreleased

### Fixed

- Validate upload identities before checksum cleanup; refuse recovered folders
  that moved, changed type/name, or went to trash.
- Preserve previously completed uploads edited before resume as conflicts;
  they must not enter failed-upload cleanup.
- Rebuild interrupted manifests without obsolete files or collision decisions.
- Preserve fast size-only skips on resume, and refresh receipts after a successful rehash.
- Reconcile changes during replacement inventory scans, detect change-cursor cycles,
  and pause cleanly on inventory quota failures.
- Reject superscript Windows device names and case variants of internal partial directories.
- Show source scan failures in reports; list healthy jobs even when another database is damaged.
- Validate account registries and OAuth client errors; close database handles on failed setup.
- Share stronger secret redaction across startup errors, logs, tracebacks and legacy reports.
- Record accurate native-build provenance and reject source/package version mismatches.

### Maintenance

- Add regression coverage for these recovery, privacy and reporting cases.
- Simplify duplicated logging setup, remove an unused retry constant/test fixture,
  use indexed collision lookups, and refresh installation/recovery guidance.

## 0.2.0

### Added

- Bidirectional `copy`, `upload`, and `download` commands with named Google accounts.
- Live terminal progress, throughput, ETA, retries, errors, and plain-log fallback.
- Durable SQLite jobs, cancellation/resume, incremental inventory and audit export.
- Verified ranged downloads, no-clobber publication and optional Google document exports.
- JSON/CSV reports, offline installation diagnostics, and binary release builds.
- Quick-start/user/developer guides, contribution and security guidance, issue/PR templates.

### Fixed

- Ambiguous upload responses reuse durable create IDs rather than creating duplicates.
- Download checkpoints survive interruptions and handle publication/status crash windows.
- Source changes, unsafe names, case collisions, expired cursors and wrong accounts fail safely.
- Retry exhaustion and private-state paths cannot produce misleading success or unsafe copies.

### Migration

`upload` now uses named accounts and SQLite jobs. Existing `sessions.json` jobs
must continue with `legacy-upload` and the original source/destination/state.
The old `auth` command remains for that compatibility mode. JSON sessions are
not imported into new jobs. See [migration notes](USAGE.md#upgrading-from-01).

### Validation limits

Offline tests and packaged-program checks cover protocol and recovery behavior.
Live Google transfers, publisher signing/notarization, and sustained multi-day
30 TB migrations are not validated by those checks.
