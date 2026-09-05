# Changelog

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
