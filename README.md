# gdrivecopy

Fast, resilient bulk upload utility for Google Drive.

Google Drive for Desktop fails with large datasets -- it caches everything on your system drive, fills it up, and stalls. gdrivecopy uploads directly via the Google Drive API with zero local staging, byte-level resume, and full metadata preservation.

## Features

- **Zero local caching** -- streams directly from source to Drive API
- **Resumable uploads** -- survives crashes, resumes on re-run
- **Stateless** -- Drive is the source of truth, run from any machine
- **Metadata preservation** -- creation date, modification date, EXIF data
- **Skip detection** -- already-uploaded files are skipped instantly
- **Integrity verification** -- MD5 checksum comparison after upload
- **Detailed logging** -- full log file plus summary report

## Prerequisites

- Python 3.10+
- A Google Cloud project with the Drive API enabled
- OAuth 2.0 credentials (`credentials.json`) from the Google Cloud Console

## Setup

```bash
pip install -r requirements.txt
gdrivecopy auth --credentials credentials.json
```

## Usage

```bash
# Upload a directory to Google Drive
gdrivecopy upload /path/to/source <drive_folder_id>

# With options
gdrivecopy upload /path/to/source <drive_folder_id> --transfers 4 --bwlimit 50M

# Dry run -- see what would be uploaded
gdrivecopy upload /path/to/source <drive_folder_id> --dry-run
```

See `gdrivecopy upload --help` for all options.

## How It Works

1. **Scan Drive** -- lists the target folder to find what's already uploaded
2. **Scan Local & Upload** -- walks the source directory, skips files already on Drive (by path + size), uploads the rest with resumable chunked uploads
3. **Report** -- prints a summary of uploaded, skipped, and failed files

Files that fail are retried automatically on the next run.

## Google Drive Limits

- **750 GB daily upload cap** per user (~24h rolling window)
- 30 TB at 750 GB/day takes ~40 days
- ~40 MB/s per-file upload speed (Google-side cap)

## License

MIT
