"""Rich terminal dashboard, plain-log fallback, and honest completion summaries."""

from __future__ import annotations

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, DownloadColumn, Progress, TaskProgressColumn, TextColumn
from rich.table import Table
from rich.text import Text

from gdrivecopy.control import ProgressModel


def size(value: int | float) -> str:
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(number) < 1024 or unit == "PiB":
            return f"{number:,.1f} {unit}"
        number /= 1024
    raise AssertionError


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "estimating"
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    prefix = f"{days}d " if days else ""
    return f"{prefix}{hours:02}:{minutes:02}:{seconds:02}"


class Dashboard:
    def __init__(
        self,
        model: ProgressModel,
        *,
        account: str,
        direction: str,
        job_id: str,
        console: Console | None = None,
        enabled: bool = True,
    ) -> None:
        self.model = model
        self.account, self.direction, self.job_id = account, direction, job_id
        self.console = console or Console(stderr=True)
        self.enabled = enabled and self.console.is_terminal
        self.live = None

    def render(self):
        snapshot = self.model.snapshot()
        headline = Text("gdrivecopy", style="bold bright_cyan")
        headline.append(f"  {self.direction.upper()}  ·  {self.account}", style="bold")
        subtitle = Text(f"Job {self.job_id}  ·  {snapshot['phase']}", style="dim")
        metrics = Table.grid(expand=True, padding=(0, 2))
        for _ in range(4):
            metrics.add_column()
        metrics.add_row("FILES", "TRANSFER RATE", "ELAPSED", "ETA")
        metrics.add_row(
            f"{snapshot['finished_files']:,} / {snapshot['total_files']:,}",
            f"{size(snapshot['rate'])}/s",
            duration(snapshot["elapsed"]),
            duration(snapshot["eta"]),
        )
        bar = Progress(
            TextColumn("[cyan]Overall"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            DownloadColumn(binary_units=True) if snapshot["total_bytes"] else TextColumn("files"),
            expand=True,
        )
        in_flight = sum(item.get("offset", 0) for item in snapshot["active"].values())
        bar.add_task(
            "",
            total=max(1, snapshot["total_bytes"] or snapshot["total_files"]),
            completed=min(snapshot["total_bytes"], snapshot["finished_bytes"] + in_flight)
            if snapshot["total_bytes"]
            else snapshot["finished_files"],
        )
        files = Table("Active file", "State", "Progress", box=box.SIMPLE, expand=True)
        files.columns[0].ratio = 3
        max_rows = max(1, min(8, self.console.height - 21))
        for path, data in list(snapshot["active"].items())[:max_rows]:
            files.add_row(
                Text(path, overflow="ellipsis", no_wrap=True),
                str(data.get("status", "Starting")),
                f"{size(data.get('offset', 0))} / {size(data.get('size', 0))}",
            )
        if not snapshot["active"]:
            files.add_row(f"{snapshot['scanned']:,} entries scanned", "Ready / scanning", "—")
        details = [headline, subtitle, Text(""), metrics, bar, files]
        if snapshot["errors"]:
            details.append(
                Panel(
                    Group(
                        *(
                            Text(error, style="red", no_wrap=True, overflow="ellipsis")
                            for error in snapshot["errors"][-2:]
                        )
                    ),
                    title="Recent issues",
                    border_style="red",
                )
            )
        details.append(
            Text(
                f"Retries {snapshot['retries']:,}  ·  This run {size(snapshot['wire_bytes'])}  ·  Ctrl+C saves progress",
                style="dim",
            )
        )
        return Panel(Group(*details), border_style="cyan", padding=(1, 2))

    def __enter__(self):
        if self.enabled:
            self.live = Live(
                console=self.console,
                get_renderable=self.render,
                refresh_per_second=4,
                transient=False,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self.live.start()
        return self

    def __exit__(self, *_args):
        if self.live:
            self.live.stop()


def render_report(report: dict, console: Console | None = None) -> None:
    console = console or Console()
    state = report["status"]
    color = (
        "green"
        if state == "complete"
        else "yellow"
        if state in {"planned", "cancelled", "paused"}
        else "red"
    )
    table = Table("Outcome", "Files", "Logical size", box=box.SIMPLE, expand=False)
    labels = {
        "copied": "Copied · checksum verified",
        "copied_unverified": "Copied · size checked only",
        "skipped_size": "Skipped · same size, content not checked",
        "skipped_verified": "Skipped · checksum matched",
        "conflict": "Conflicts · untouched",
        "failed": "Failed",
        "unsupported": "Unsupported items",
        "pending": "Pending",
        "running": "Interrupted",
        "cancelled": "Cancelled",
        "exported": "Exported · converted Google document",
    }
    for status, counts in report["counts"].items():
        table.add_row(labels.get(status, status), f"{counts['files']:,}", size(counts["bytes"]))
    console.print(
        Panel(
            Group(
                Text(f"{state.upper()}  ·  {report['job_id']}", style=f"bold {color}"),
                Text(f"{report['direction']}  ·  {report['account_email']}"),
                table,
                Text(
                    f"This run: {size(report['bytes_this_run'])} transferred · {duration(report['duration_seconds'])}"
                ),
                *(
                    Text(report["stop_reason"], style="yellow")
                    for _ in [0]
                    if report.get("stop_reason")
                ),
                Text(
                    f"Saved transfers: {size(report.get('avoided_bytes', 0))} skipped · {size(report.get('resumed_bytes_this_run', 0))} resumed · {report.get('retries', 0):,} retries across runs"
                ),
                *(
                    Text(item, style="red")
                    for item in ([report["fatal_error"]] if report.get("fatal_error") else [])
                ),
                *(
                    Text(f"{item['path']}: {item['error']}", style="red")
                    for item in report.get("errors_sample", [])[:5]
                ),
                *(Text(item, style="dim") for item in report.get("limitations", [])),
                Text(
                    f'Resume: gdrivecopy resume {report["job_id"]} --state-dir "{report["state_dir"]}"'
                ),
                Text(f"Reports and audit: {report['directory']}"),
            ),
            title="Copy report",
            border_style=color,
        )
    )
