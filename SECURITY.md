# Security policy

Security fixes target the latest release. Older versions may need an upgrade.
There is no guaranteed response time or commercial support commitment.

Report vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/sytelus/gdrivecopy/security/advisories/new).
Include affected versions, impact, and a minimal synthetic reproduction.
Do not publish an exploit or sensitive account data in a public issue.

Never share OAuth client/token files, resumable upload URLs, or job databases.
Reports and logs can include filenames and email addresses; redact them before
sharing. Download releases only from this repository and compare their SHA-256
hashes with `SHA256SUMS`. Hashes detect changed downloads, not publisher identity.
Current binaries are not publisher-signed or notarized.

The app assumes trusted local state and destination directories without hostile
concurrent writers. MD5 verification detects ordinary corruption; it is not a
cryptographic authenticity guarantee. See [OPERATIONS.md](OPERATIONS.md) for
scope and recovery limits.
