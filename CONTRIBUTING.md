# Contributing

Bug reports, documentation improvements and focused pull requests are welcome.
For a large change, open an issue first so the behavior and scope are clear.
Please follow the [community guidelines](CODE_OF_CONDUCT.md).

## Local setup

```sh
git clone https://github.com/sytelus/gdrivecopy.git
cd gdrivecopy
python -m venv .venv
# Activate: .venv\Scripts\Activate.ps1 on Windows; source .venv/bin/activate on Unix
python -m pip install -e ".[dev]"
python -m ruff format .
python -m ruff check .
python -m pytest
```

Tests are offline. Never use a real OAuth token in an automated test. Include
a regression test for a correctness/recovery bug, and update help/docs when
behavior changes. Keep pull requests focused and explain what changed and how
you checked it. See [DEVELOPMENT.md](DEVELOPMENT.md) for architecture, invariants,
packaging and release checks.

## Reporting problems

Include the app version, operating system, installation method, a redacted
command, expected/actual behavior, and `gdrivecopy doctor --json` output. A small
synthetic reproduction is ideal. Review logs/reports for private filenames and
email addresses. **Do not attach credentials, tokens, job databases, or resumable
URLs.** Report vulnerabilities privately using [SECURITY.md](SECURITY.md).

Contributions are licensed under the project's [MIT license](LICENSE).
