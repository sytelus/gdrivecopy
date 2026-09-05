# Product contract — gdrivecopy 0.2

## Purpose and workflow

Copy folder contents between local filesystems and Google My Drive through a
terminal. Prioritize correct outcomes, recovery and throughput. The intended
workload includes multi-day 5–30 TB and larger trees; design targets must not be
presented as measured live capacity.

Users add named accounts, explicitly choose a profile/default, and run `copy`,
`upload`, or `download`. Display server-verified email, save a job ID before work,
show scanning/transfer progress, and support plain output. Ctrl+C stops safely;
`resume JOB_ID` uses the same Google user after interruption/reboot. Provide a
human summary, JSON statistics, per-file CSV, rotating logs and audit export.

## Correctness contract

- New binaries default to full-file Drive MD5 verification. Download final names
  appear only after range validation, synced partial data, checksum and source
  metadata checks. Size-only verification is explicit and separately labeled.
- Existing same-size items are a separately reported performance shortcut.
  Optional local hashing strengthens comparison without fetching remote payloads.
- Never overwrite/delete pre-existing destinations. Cleanup may trash only a
  newly created unverified upload; failed cleanup stops that file.
- Persist generated file/folder IDs before creates. Ambiguous responses retry
  the same ID to prevent duplicate creation.
- Bind jobs to actual Google user/root, absolute local path, and a fixed source
  manifest. Changed source identity/size/mtime/version cannot silently join a
  different transfer.
- Report unreadable files, unsafe/duplicate paths, file/folder collisions,
  permissions failures and unsupported items. Incomplete listing is not success.
- Google-native exports are conversions with distinct outcomes. Their local
  checksums are recovery receipts, not source verification.

## Performance and recovery contract

- Store manifests in indexed SQLite; scan/dispatch in bounded batches and keep
  only active workers/recent samples in the dashboard.
- Configure concurrency, buffers and payload pacing. Use per-worker payload
  connections and serialize the discovery metadata client for thread safety.
- Query resumable upload status before continuing. Download offset checkpoints
  follow flush/fsync. Save verification receipts before no-clobber publication.
- Reuse fixed manifests and change cursors; refresh affected folders and recover
  expired cursors. Listings are not transactional snapshots.
- Retry transient errors with bounded cancellable backoff. Stop scheduling on
  cancellation, blocking quota or repeated failures. Resume is explicit.
- OS-owned locks prevent concurrent writers to one job and release on crashes.
- Required identity/checkpoint writes are mandatory. Reports/logs/audit must not
  expose OAuth tokens, headers, or resumable capabilities.

## Limits and acceptance evidence

No shared drives, Drive-to-Drive/local-to-local copies, overwrite/mirror/delete,
empty-directory preservation, link following, permissions/history backup,
automatic boot scheduling, distributed jobs or billing estimates. Retain the
legacy JSON uploader for existing sessions with documented limits.

Resume reuses completed local copies by size/mtime receipt; a new checksum
comparison job detects later silent corruption. Verified partial files reread
their local prefix to reconstruct portable full-file MD5 state.

Offline tests must cover HTTP response validation, account isolation, ambiguous
creates, interrupted/reopened jobs, publication crash windows, source mutation,
no-clobber behavior, cursor recovery, large byte counts, bounded dispatch and
nonzero incomplete outcomes. CI runs Windows/Linux, Python 3.10/3.14. Live
disposable-account tests and multi-day scale measurements are required before
asserting production throughput or end-to-end 30 TB reliability.
