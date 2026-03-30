# gdrivecopy

Fast, resilient bulk upload utility for Google Drive.

## What This Tool Does

Google Drive for Desktop fails at scale. It stages every file through a hidden cache on your system drive, regardless of where the files actually live. With large datasets -- hundreds of gigabytes, hundreds of thousands of files -- the cache fills your system drive, Windows becomes unstable, and the sync silently stalls. Uploads above roughly 500 GB are impractical.

gdrivecopy solves this by uploading directly via the Google Drive API with zero local caching. It streams files straight from your disk to Drive, supports byte-level resume if interrupted, preserves file creation and modification dates, and verifies integrity with MD5 checksums after every upload. It is stateless: Drive itself is the source of truth, so you can run the tool from any machine at any time.

## Prerequisites

- **Python 3.10 or newer** -- check with `python --version` or `python3 --version`
- **A Google account** with Google Drive storage (the free 15 GB tier works; a Google One plan is needed for larger uploads)

## Google Cloud Setup

gdrivecopy talks directly to the Google Drive API, which means you need to create a small "project" in the Google Cloud Console and download a credentials file. This is a one-time setup that takes about five minutes.

### 1. Create a Google Cloud project

1. Go to [https://console.cloud.google.com/](https://console.cloud.google.com/) and sign in with your Google account.
2. Click the project dropdown at the top of the page (it may say "Select a project" or show an existing project name).
3. Click **New Project**.
4. Give it any name you like (for example, `gdrivecopy`). Leave the organization and location as defaults.
5. Click **Create** and wait a few seconds for the project to be ready.
6. Make sure your new project is selected in the project dropdown.

### 2. Enable the Google Drive API

1. In the left sidebar, go to **APIs & Services** then **Library** (or search for "API Library" in the top search bar).
2. Search for **Google Drive API**.
3. Click on **Google Drive API** in the results.
4. Click **Enable**. Wait for it to activate.

### 3. Configure the OAuth consent screen

Before you can create credentials, Google requires you to configure a consent screen. This is the screen you see when the tool asks you to sign in.

1. In the left sidebar, go to **APIs & Services** then **OAuth consent screen**.
2. Select **External** as the user type and click **Create**.
3. Fill in the required fields:
   - **App name**: anything you like (for example, `gdrivecopy`)
   - **User support email**: select your email address
   - **Developer contact information**: enter your email address
4. Click **Save and Continue** through the remaining steps (Scopes, Test Users, Summary). You can leave the defaults.
5. On the **Test users** step, click **Add Users** and enter the Gmail address you will use with gdrivecopy. Click **Save and Continue**.
6. Click **Back to Dashboard**.

### 4. Create OAuth credentials

1. In the left sidebar, go to **APIs & Services** then **Credentials**.
2. Click **Create Credentials** at the top, then select **OAuth client ID**.
3. For **Application type**, select **Desktop app**.
4. Give it any name (for example, `gdrivecopy`).
5. Click **Create**.
6. A dialog appears with your client ID and secret. Click **Download JSON**.
7. Save the downloaded file as `credentials.json` in the directory where you will run gdrivecopy.

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/gdrivecopy.git
cd gdrivecopy
pip install -e .
```

This installs gdrivecopy and all its dependencies. The `-e` flag installs in "editable" mode so you can pull updates with `git pull` without reinstalling.

After installation, the `gdrivecopy` command is available in your terminal.

## Authentication

Run the auth command once to link gdrivecopy to your Google account:

```bash
gdrivecopy auth --credentials credentials.json
```

This opens your default web browser. Sign in with your Google account, review the permissions, and click **Allow**. You may see a warning that the app is unverified -- click **Advanced** then **Go to gdrivecopy (unsafe)** to proceed. This is expected for personal projects that have not been submitted to Google for review.

Once you grant access, a `token.json` file is saved in the current directory. Future runs use this token automatically and will not prompt you to sign in again.

## Finding Your Drive Folder ID

You need the ID of the Google Drive folder where files will be uploaded.

1. Open [Google Drive](https://drive.google.com/) in your browser.
2. Navigate to the folder you want to upload into (or create a new one).
3. Look at the URL in your browser's address bar. It looks like this:
   ```
   https://drive.google.com/drive/folders/1aBcDeFgHiJkLmNoPqRsTuVwXyZ
   ```
4. The folder ID is the long string after `/folders/` -- in this example, `1aBcDeFgHiJkLmNoPqRsTuVwXyZ`.

Copy that string. You will pass it to gdrivecopy as the `<drive_folder_id>` argument.

## Usage

### Basic upload

Upload an entire directory tree to a Drive folder:

```bash
gdrivecopy upload /path/to/my/photos 1aBcDeFgHiJkLmNoPqRsTuVwXyZ
```

The tool scans Drive to find what is already uploaded, then uploads only the new files. The full directory structure is recreated on Drive.

### Dry run

See what would be uploaded without actually uploading anything:

```bash
gdrivecopy upload /path/to/my/photos 1aBcDeFgHiJkLmNoPqRsTuVwXyZ --dry-run
```

### Limit upload bandwidth

Cap the upload speed to avoid saturating your internet connection (useful if you need to keep browsing while uploading):

```bash
gdrivecopy upload /path/to/my/photos 1aBcDeFgHiJkLmNoPqRsTuVwXyZ --bwlimit 50M
```

The value accepts `K` (kilobytes/s), `M` (megabytes/s), or `G` (gigabytes/s).

### More concurrent uploads

By default, gdrivecopy uploads 4 files at the same time. You can increase this for faster throughput (at the cost of more memory and network usage):

```bash
gdrivecopy upload /path/to/my/photos 1aBcDeFgHiJkLmNoPqRsTuVwXyZ --transfers 8
```

### Specify credential and token locations

If your credentials or token files are in a different directory:

```bash
gdrivecopy upload /path/to/my/photos 1aBcDeFgHiJkLmNoPqRsTuVwXyZ \
    --credentials /path/to/credentials.json \
    --token /path/to/token.json
```

### All options

```
gdrivecopy upload <source_dir> <drive_folder_id> [options]

  --transfers N          Concurrent uploads (default: 4)
  --chunk-size SIZE      Upload chunk size (default: 64M, must be multiple of 256K)
  --bwlimit RATE         Max upload speed, e.g. 50M (default: unlimited)
  --dry-run              Scan only, report what would be uploaded
  --log-dir PATH         Directory for log files (default: current directory)
  --log-level LEVEL      DEBUG, INFO, WARNING, or ERROR (default: INFO)
  --credentials PATH     OAuth credentials JSON (default: ./credentials.json)
  --token PATH           Cached OAuth token (default: ./token.json)
  --sessions PATH        Session cache file (default: ./sessions.json)
  --no-checksum-verify   Skip post-upload MD5 verification
  --wait-on-limit        Wait and auto-resume when daily limit is hit (default: exit)
  --quiet                Minimal output
```

## How It Works

gdrivecopy runs in three phases:

1. **Scan Drive** -- Lists everything in the target Drive folder recursively and builds an in-memory map of what is already uploaded (path, size, and checksum for each file). This takes about 20-30 seconds for 500,000 files.

2. **Scan Local and Upload** -- Walks your source directory. For each file, it checks the Drive map: if the file already exists with the same size, it is skipped instantly. If the file is missing from Drive, it is uploaded. Files larger than 8 MB use Google's resumable upload protocol, which streams data in 64 MB chunks and can resume from the exact byte if interrupted. Files 8 MB or smaller are uploaded in a single request. After each upload, the tool verifies the file's integrity by comparing MD5 checksums.

3. **Report** -- Prints a summary of how many files were uploaded, skipped, resumed, and failed, along with total bytes transferred and elapsed time.

Files that fail due to transient errors (network issues, server errors) are retried up to 8 times with exponential backoff. Files that still fail are simply retried on the next run -- since they are not on Drive, the tool will pick them up again automatically.

## Session Cache

gdrivecopy creates a `sessions.json` file in the current directory to track in-progress uploads. This enables byte-level resume: if a 50 GB upload is interrupted at 49 GB, it picks up from byte 49 GB on the next run instead of starting over.

This file is a disposable optimization, not essential state. You can safely delete it at any time. Without it, any interrupted uploads simply restart from the beginning. The tool will never lose data or produce duplicates regardless of whether this file exists.

## Google Drive Limits

Google enforces a **750 GB daily upload cap** per user on a rolling 24-hour window. There is no way around this limit.

When the daily cap is reached, gdrivecopy stops uploading and reports the situation. You can re-run the tool after approximately 24 hours and it will pick up exactly where it left off (all previously uploaded files are skipped automatically).

For large datasets, plan accordingly:

| Dataset Size | Approximate Days |
|--------------|-----------------|
| 750 GB       | 1 day           |
| 5 TB         | ~7 days         |
| 15 TB        | ~20 days        |
| 30 TB        | ~40 days        |

Other limits to be aware of:

- **Per-file speed**: roughly 40 MB/s (Google-side cap)
- **Maximum file size**: 5 TB per file
- **Resumable session lifetime**: 1 week (sessions older than this expire and the upload restarts)

## Troubleshooting

### "OAuth credentials not found"

The tool cannot find `credentials.json`. Make sure you downloaded the OAuth client JSON from the Google Cloud Console (see [Google Cloud Setup](#google-cloud-setup)) and that you are either running gdrivecopy from the same directory as the file, or passing `--credentials /path/to/credentials.json`.

### "Daily upload limit (750 GB) reached"

This is normal for large uploads. Google enforces a 750 GB per-day cap. Wait approximately 24 hours and run the same command again. The tool will skip everything already uploaded and continue with the remaining files.

### Browser does not open during authentication

If the browser does not open automatically when running `gdrivecopy auth`, check the terminal output for a URL. Copy and paste that URL into your browser manually to complete the sign-in flow.

### "Access blocked: This app's request is invalid" or "Error 403: access_denied"

This usually means the OAuth consent screen is not configured correctly. Go back to the Google Cloud Console, navigate to **APIs & Services** then **OAuth consent screen**, and make sure:
- Your email address is listed as a **test user**.
- The Google Drive API is enabled for your project.

### "Token has been expired or revoked"

Delete the `token.json` file and run `gdrivecopy auth --credentials credentials.json` again to re-authenticate.

### "Source directory does not exist"

Double-check the path you passed as the first argument. The path must point to an existing directory on your local machine. Use an absolute path to avoid ambiguity (for example, `/home/user/photos` instead of `~/photos`).

### Upload seems stuck or slow

- Google caps per-file upload speed at roughly 40 MB/s. This is a server-side limit.
- If you are uploading many small files, the overhead of individual API calls can slow things down. This is expected.
- Check the log file (`gdrivecopy_*.log` in the current directory or `--log-dir`) for warnings or errors.

### Size mismatch warnings

If the tool reports "size mismatch" for a file, it means the file exists on Drive but with a different size than the local copy. The tool skips these files and logs a warning. This can happen if a file was modified locally after a previous upload, or if a previous upload was corrupted. Review the mismatches in the log file and handle them manually if needed.

## License

MIT
