# Running and recovering multi-day copies

## Planning

Keep application state on a reliable local disk, credentials private, and the
account explicit. Keep source/destination stable: directory walks and Drive
listings are not snapshots. Check destination capacity and filesystem file-size
limits. Tiny files incur API/metadata overhead; quotas can dominate bandwidth.

Start with 4 transfers and 64M chunks. Increase concurrency only if disk,
network, CPU and quotas allow it; lower buffers/concurrency if memory is tight.
Parts live inside the destination filesystem so final publication does not
require another full staging copy.

Google documents a 750 GB daily upload limit for Workspace users and a maximum
uploaded file size of 5 TB. New/older Cloud projects can have different API
quotas and billing thresholds. Review the project's
[current Google Drive limits](https://developers.google.com/workspace/drive/api/guides/limits)
before a 5–30 TB job. The app cannot bypass limits or predict charges. ETA
excludes future quota pauses, downtime and unknown export sizes.

## State to retain

Keep the whole state directory, referenced OAuth client JSON, original local
paths and `.gdrivecopy-JOB_ID.parts` directories. Do not copy only `job.sqlite3`
while running: transactions may be in `job.sqlite3-wal`. Stop before backing up
state. Restoring metadata without its corresponding payload is not a verified backup.

State contains credentials/capabilities; reports retain names and email addresses.
Review reports before sharing. Audit/log messages redact common session/token
fields but are diagnostic, not tamper-evident.

## Recovery

| Situation | Action and behavior |
|---|---|
| Ctrl+C | Wait for active requests to finish/time out; resume the printed ID. |
| Crash / forced exit / Windows update | Run `jobs`, then `resume JOB_ID`; OS locks release automatically. A stale `running` label is an unfinished run, not proof it is alive. |
| Network/rate error | Bounded backoff retries; resume after repeated failures once connectivity recovers. |
| Blocking quota | Inspect account/API/storage limits; explicitly resume after correction/reset. |
| Revoked/expired token | Sign in again when prompted with the original Google user. |
| Missing/short partial | Restart that file from zero; retain completed verified files. |
| Expired upload session | Recover a completed reserved ID or start a fresh session; discarded server prefixes may need retransmission. |
| Expired listing cursor | Rescan that folder; an expired change cursor requires rebuilding inventory. |
| Existing destination conflict | Review/move the item yourself, then resume. Static namespace collisions need fixed Drive names and a new job. |
| Changed source/new files | Start a new copy job; resume retains the original manifest. |
| Suspected later disk corruption | Start a new download job with `--existing checksum`; mismatches are reported without overwrite. |

Cancellation is cooperative. Raw HTTP uses a 10-second connect timeout and a
300-second read timeout; a responsive slow stream may keep a request active
longer. Retry/pacing waits stop promptly. Forced termination can omit final
timing/report updates and require checking or retransmitting uncheckpointed bytes.

## Reading reports

`report.json` summarizes an ended run; `files.csv` covers source path, target,
logical size, status and error. Both are atomically replaced. After an abrupt
crash, `report JOB_ID` reads current database state. During a running job, counts
can change between queries and last-run timing fields are not live counters.

`bytes_this_run` counts confirmed payload progress, including partial files,
not exact wire traffic. Lost acknowledgements, retransmits, protocol overhead
and abrupt death can differ from billed/network bytes. `resumed_bytes_this_run`
counts saved prefixes of files completed this run. `avoided_bytes` counts
existing-file skips. Retry counts cover audit events across attempts; durations
exclude stopped time. Native export sizes become known only after export.

The overall bar means planned bytes processed, including skips and failures;
**it is not a verification percentage**. Use the outcome table and exit code.

```powershell
gdrivecopy report JOB_ID --json
gdrivecopy report JOB_ID --audit-output "E:\support\job-audit.jsonl"
```

Audit records phases, file starts/outcomes, retries and run boundaries. Byte
offsets are checkpoints rather than one event per chunk. Logs rotate at 10 MiB
with five backups; the full audit remains in SQLite and grows with work/retries.
Reserve space for state, journals, audit and report exports.

## Preservation limits

No empty directories, ACLs/ownership, sharing, comments, revisions, shortcuts or
links are preserved. Exports cannot retain complete native document semantics.
Portable path checks reject unsafe names and directory aliases on all OSes;
they never silently rename content. A trusted destination without competing
writers is assumed; path checks are not a sandbox against malicious processes.
