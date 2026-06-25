"""
NiChart CLI — terminal client for the NiChart API.

Typical usage
-------------
    nichart status
    nichart projects create myproject
    nichart files upload nifti myproject scan_T1.nii.gz
    nichart pipelines list
    nichart jobs submit myproject dummy_pipeline --param duration_seconds=5
    nichart jobs                          # live dashboard of all your jobs
    nichart jobs <run_id>                 # live detail view for one run
    nichart jobs logs <run_id>

Server URL is read from the NICHART_API_URL environment variable
(default: http://localhost:8000).  Override per-command with --url.
"""

from __future__ import annotations

import getpass
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich import box
from rich.console import Console
from rich.live import Live
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

# ── App skeleton ──────────────────────────────────────────────────────────────

console = Console()

app = typer.Typer(
    name="nichart",
    help="NiChart — submit and monitor medical-imaging pipeline jobs.",
    no_args_is_help=True,
    add_completion=False,
)
projects_app = typer.Typer(no_args_is_help=True, help="Manage projects.")
files_app    = typer.Typer(no_args_is_help=True, help="Upload, list, and download project files.")
pipelines_app = typer.Typer(no_args_is_help=True, help="Browse available pipelines.")
jobs_app     = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help=(
        "Submit and monitor pipeline jobs.\n\n"
        "With no arguments, shows a live dashboard of all your runs.\n"
        "Pass a run ID to watch a specific run."
    ),
)

app.add_typer(projects_app, name="projects")
app.add_typer(files_app,    name="files")
app.add_typer(pipelines_app, name="pipelines")
app.add_typer(jobs_app,     name="jobs")

# Global URL state (set by --url callback)
_api_url: str = ""


@app.callback()
def _global(
    url: str = typer.Option(
        "",
        "--url",
        envvar="NICHART_API_URL",
        help="NiChart API base URL.",
        show_default="http://localhost:8000",
    ),
) -> None:
    global _api_url
    _api_url = url.rstrip("/") if url else "http://localhost:8000"


# ── API client ────────────────────────────────────────────────────────────────

def _api(
    method: str,
    path: str,
    silent_errors: bool = False,
    **kwargs,
) -> dict | list:
    try:
        r = httpx.request(method, f"{_api_url}{path}", timeout=60, **kwargs)
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {_api_url}[/red] — is the server running?")
        raise typer.Exit(1)
    if not r.is_success:
        if not silent_errors:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            console.print(f"[red]Error {r.status_code}:[/red] {detail}")
            raise typer.Exit(1)
        raise typer.Exit(1)
    return r.json()


def _api_download(path: str) -> httpx.Response:
    try:
        return httpx.stream("GET", f"{_api_url}{path}", timeout=120, follow_redirects=True)
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {_api_url}[/red]")
        raise typer.Exit(1)


# ── Formatting helpers ─────────────────────────────────────────────────────────

_STATUS_STYLE = {
    "pending":   "yellow",
    "running":   "cyan",
    "succeeded": "green",
    "failed":    "red",
    "skipped":   "dim",
    "cancelled": "dim",
}

VALID_MODALITIES = ("t1", "fl", "t2", "t1ce", "adc")


def _status_text(status: str) -> Text:
    return Text(status, style=_STATUS_STYLE.get(status, "white"))


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%d %H:%M")


def _elapsed(submitted: str | None, finished: str | None) -> str:
    if not submitted:
        return "—"
    start = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
    end = (
        datetime.fromisoformat(finished.replace("Z", "+00:00"))
        if finished
        else datetime.now(timezone.utc)
    )
    s = int((end - start).total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _project_root(project: str) -> Path | None:
    """Return absolute host path to a project directory, if NICHART_DATA_ROOT is set."""
    root = os.environ.get("NICHART_DATA_ROOT")
    if root:
        return Path(root) / getpass.getuser() / project
    return None


# ── nichart status ─────────────────────────────────────────────────────────────

@app.command()
def status() -> None:
    """Check server health and show connection details."""
    data = _api("GET", "/health")
    table = Table(show_header=False, box=box.SIMPLE)
    table.add_row("Server", _api_url)
    table.add_row("Status", Text(data["status"], style="green" if data["status"] == "ok" else "red"))
    table.add_row("Mode", data.get("execution_mode", "—"))
    table.add_row("Version", data.get("version", "—"))
    console.print(table)


# ── nichart data <project> ────────────────────────────────────────────────────

@app.command()
def data(
    project: str = typer.Argument(..., help="Project name."),
) -> None:
    """Print the absolute host path to a project's data directory."""
    path = _project_root(project)
    if path:
        console.print(str(path))
    else:
        console.print(
            "[yellow]NICHART_DATA_ROOT is not set.[/yellow] "
            "Set it to the server's data root to resolve absolute paths."
        )


# ── nichart projects ──────────────────────────────────────────────────────────

@projects_app.command("list")
def projects_list() -> None:
    """List all your projects."""
    items = _api("GET", "/projects")
    if not items:
        console.print("[dim]No projects yet. Create one with: nichart projects create <name>[/dim]")
        return
    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("Name", style="bold")
    table.add_column("Created")
    for p in items:
        table.add_row(p["id"], _fmt_dt(p.get("created_at")))
    console.print(table)


@projects_app.command("create")
def projects_create(
    name: str = typer.Argument(..., help="Project name (alphanumeric, hyphens, underscores)."),
) -> None:
    """Create a new project."""
    p = _api("POST", "/projects", json={"name": name})
    console.print(f"[green]Created[/green] project [bold]{p['id']}[/bold]")
    path = _project_root(name)
    if path:
        console.print(f"[dim]Data directory:[/dim] {path}")


@projects_app.command("delete")
def projects_delete(
    name: str = typer.Argument(..., help="Project name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a project and all its data."""
    if not yes:
        if not Confirm.ask(f"Delete project [bold]{name}[/bold] and all its data?", default=False):
            raise typer.Exit(0)
    _api("DELETE", f"/projects/{name}")
    console.print(f"[green]Deleted[/green] project [bold]{name}[/bold]")


# ── nichart files ─────────────────────────────────────────────────────────────

@files_app.command("list")
def files_list(
    project: str = typer.Argument(..., help="Project name."),
) -> None:
    """List files in a project."""
    path = _project_root(project)
    if path:
        console.print(f"[dim]Project data:[/dim] {path}\n")

    result = _api("GET", f"/projects/{project}/files")
    entries = result.get("entries", [])

    if not entries:
        console.print("[dim]No files yet.[/dim]")
        return

    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("Path", style="bold")
    table.add_column("Type", style="dim")
    table.add_column("Size", justify="right")
    for e in sorted(entries, key=lambda x: x["path"]):
        table.add_row(
            e["path"],
            e["type"],
            _fmt_size(e.get("size")) if e["type"] == "file" else "",
        )
    console.print(table)
    console.print(f"[dim]{len(entries)} entries[/dim]")


@files_app.command("download")
def files_download(
    project: str = typer.Argument(..., help="Project name."),
    path: str = typer.Argument(..., help="Relative path within the project."),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="Output path (default: current directory)."),
    zip: bool = typer.Option(False, "--zip", help="Download a directory as a zip archive."),
) -> None:
    """Download a file or directory (--zip) from a project."""
    query = f"?path={path}"
    if zip:
        query += "&zip=true"
    dest = Path(out) if out else Path(Path(path).name + (".zip" if zip else ""))

    with _api_download(f"/projects/{project}/files/download{query}") as r:
        if not r.is_success:
            console.print(f"[red]Error {r.status_code}[/red]")
            raise typer.Exit(1)
        total = int(r.headers.get("content-length", 0))
        written = 0
        with dest.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)
                written += len(chunk)
        console.print(f"[green]Saved[/green] {dest} ({_fmt_size(written)})")


@files_app.command("delete")
def files_delete(
    project: str = typer.Argument(..., help="Project name."),
    path: str = typer.Argument(..., help="Relative path within the project."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a file or directory from a project."""
    if not yes:
        if not Confirm.ask(f"Delete [bold]{path}[/bold] from project [bold]{project}[/bold]?", default=False):
            raise typer.Exit(0)
    _api("DELETE", f"/projects/{project}/files", params={"path": path})
    console.print(f"[green]Deleted[/green] {path}")


# ── NIfTI upload (interactive staging flow) ───────────────────────────────────

@files_app.command("upload-nifti")
def files_upload_nifti(
    project: str = typer.Argument(..., help="Project name."),
    files: list[Path] = typer.Argument(..., help="NIfTI files (.nii or .nii.gz)."),
) -> None:
    """Upload NIfTI files with interactive MRID/modality confirmation."""
    upload_files = [
        ("files", (f.name, f.open("rb"), "application/octet-stream"))
        for f in files
        if f.is_file()
    ]
    if not upload_files:
        console.print("[red]No valid files provided.[/red]")
        raise typer.Exit(1)

    console.print(f"Uploading {len(upload_files)} file(s)…")
    resp = _api(
        "POST", f"/projects/{project}/files/upload/nifti",
        files=upload_files,
    )
    for _, (_, fh, _) in upload_files:
        fh.close()

    staging_id: str = resp["staging_id"]
    proposals: list[dict] = resp["proposals"]

    # Show inferred proposals
    table = Table(box=box.SIMPLE_HEAVY, title="Inferred mappings")
    table.add_column("Filename")
    table.add_column("MRID")
    table.add_column("Modality")
    for p in proposals:
        mrid_text = Text(p["inferred_mrid"] or "[red]?[/red]")
        mod_text  = Text(p["inferred_modality"] or "[red]?[/red]")
        table.add_row(p["filename"], mrid_text, mod_text)
    console.print(table)

    has_unknowns = any(
        not p["inferred_mrid"] or not p["inferred_modality"]
        for p in proposals
    )
    if has_unknowns:
        console.print("[yellow]Some fields could not be inferred — you must fill them in.[/yellow]")

    # Ask: commit / edit / discard
    choices = ["y", "e", "n"] if not has_unknowns else ["e", "n"]
    default = "e" if has_unknowns else "y"
    choice = Prompt.ask(
        "Commit? [[green]y[/green]]es / [[yellow]e[/yellow]]dit / [[red]n[/red]]o (discard)",
        choices=choices,
        default=default,
    ).lower()

    if choice == "n":
        _api("DELETE", f"/projects/{project}/files/stage/{staging_id}")
        console.print("[dim]Staged files discarded.[/dim]")
        return

    mappings = []
    if choice == "e" or has_unknowns:
        console.print("\nEdit each mapping (press Enter to accept default):\n")
        for p in proposals:
            console.print(f"[bold]{p['filename']}[/bold]")
            mrid = Prompt.ask("  MRID", default=p["inferred_mrid"] or "")
            while not mrid.strip():
                console.print("  [red]MRID cannot be empty.[/red]")
                mrid = Prompt.ask("  MRID")
            mod = Prompt.ask(
                f"  Modality ({'/'.join(VALID_MODALITIES)})",
                default=p["inferred_modality"] or "",
            )
            while mod not in VALID_MODALITIES:
                console.print(f"  [red]Must be one of: {', '.join(VALID_MODALITIES)}[/red]")
                mod = Prompt.ask(f"  Modality ({'/'.join(VALID_MODALITIES)})")
            mappings.append({"filename": p["filename"], "mrid": mrid.strip(), "modality": mod})
    else:
        mappings = [
            {"filename": p["filename"], "mrid": p["inferred_mrid"], "modality": p["inferred_modality"]}
            for p in proposals
        ]

    result = _api(
        "POST",
        f"/projects/{project}/files/stage/{staging_id}/commit",
        json={"mappings": mappings},
    )
    committed = result.get("committed", [])
    console.print(f"[green]Committed {len(committed)} file(s).[/green]")
    for c in committed:
        console.print(f"  [dim]{c['modality']}/{c['mrid']}.nii.gz[/dim]")


@files_app.command("upload-csv")
def files_upload_csv(
    project: str = typer.Argument(..., help="Project name."),
    file: Path = typer.Argument(..., help="Participants CSV file."),
) -> None:
    """Upload a participants CSV (overwrites existing)."""
    _api(
        "POST", f"/projects/{project}/files/upload/csv",
        files={"file": (file.name, file.open("rb"), "text/csv")},
    )
    console.print(f"[green]Uploaded[/green] participants CSV from {file.name}")


@files_app.command("upload-bids")
def files_upload_bids(
    project: str = typer.Argument(..., help="Project name."),
    file: Path = typer.Argument(..., help="BIDS dataset zip archive."),
) -> None:
    """Upload a BIDS zip archive (reorganised into NiChart layout automatically)."""
    _api(
        "POST", f"/projects/{project}/files/upload/bids",
        files={"file": (file.name, file.open("rb"), "application/zip")},
    )
    console.print(f"[green]Uploaded[/green] BIDS archive {file.name}")


@files_app.command("upload-idat")
def files_upload_idat(
    project: str = typer.Argument(..., help="Project name."),
    file: Path = typer.Argument(..., help="IDAT zip archive."),
) -> None:
    """Upload an IDAT zip archive."""
    _api(
        "POST", f"/projects/{project}/files/upload/idat",
        files={"file": (file.name, file.open("rb"), "application/zip")},
    )
    console.print(f"[green]Uploaded[/green] IDAT archive {file.name}")


# ── nichart pipelines ─────────────────────────────────────────────────────────

@pipelines_app.command("list")
def pipelines_list() -> None:
    """List all available pipelines."""
    items = _api("GET", "/catalog/pipelines")
    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("ID", style="bold")
    table.add_column("Name")
    table.add_column("Categories", style="dim")
    table.add_column("Description")
    for p in items:
        table.add_row(
            p["id"],
            p["name"],
            ", ".join(p.get("categories") or []),
            (p.get("description") or "")[:60],
        )
    console.print(table)


@pipelines_app.command("show")
def pipelines_show(
    pipeline_id: str = typer.Argument(..., help="Pipeline ID."),
) -> None:
    """Show pipeline details, steps, and parameters."""
    p = _api("GET", f"/catalog/pipelines/{pipeline_id}")
    console.print(f"\n[bold]{p['name']}[/bold]  [dim]({p['id']})[/dim]")
    if p.get("description"):
        console.print(p["description"])

    steps = p.get("steps") or []
    if steps:
        console.print(f"\n[underline]Steps[/underline] ({len(steps)})")
        for i, s in enumerate(steps, 1):
            console.print(f"  {i}. [bold]{s['id']}[/bold]  tool: {s['tool']}")

    params = p.get("parameters") or {}
    if params:
        console.print("\n[underline]Parameters[/underline]")
        pt = Table(box=box.SIMPLE, show_header=True)
        pt.add_column("Name", style="bold")
        pt.add_column("Type")
        pt.add_column("Default")
        pt.add_column("Range / Choices")
        pt.add_column("Description")
        for name, spec in params.items():
            choices = spec.get("choices")
            lo, hi = spec.get("min"), spec.get("max")
            if choices:
                constraint = " | ".join(str(c) for c in choices)
            elif lo is not None or hi is not None:
                constraint = f"{lo if lo is not None else ''}…{hi if hi is not None else ''}"
            else:
                constraint = ""
            pt.add_row(
                name,
                spec.get("type", ""),
                str(spec.get("default", "")),
                constraint,
                spec.get("description") or "",
            )
        console.print(pt)

    requires = p.get("requires") or []
    if requires:
        console.print("\n[underline]Requirements[/underline]")
        for r in requires:
            console.print(f"  • {r}")
    console.print()


# ── nichart readiness ─────────────────────────────────────────────────────────

@app.command()
def readiness(
    project: str = typer.Argument(..., help="Project name."),
    pipeline_id: str = typer.Argument(..., help="Pipeline ID."),
) -> None:
    """Check whether a project has the data needed to run a pipeline."""
    report = _api("GET", f"/projects/{project}/readiness/{pipeline_id}")
    satisfied = report.get("satisfied", False)
    badge = Text("READY", style="green") if satisfied else Text("NOT READY", style="red")
    console.print(f"\nProject [bold]{project}[/bold] → pipeline [bold]{pipeline_id}[/bold]: {badge}\n")

    # Imaging modality checks
    for img in report.get("imaging") or []:
        icon = "[green]✓[/green]" if img["satisfied"] else "[red]✗[/red]"
        console.print(
            f"  {icon}  {img['modality'].upper()} imaging — "
            f"{img['subject_count']} subject(s)"
        )

    # CSV column checks
    csv = report.get("csv")
    if csv:
        csv_icon = "[green]✓[/green]" if csv["satisfied"] else "[red]✗[/red]"
        console.print(
            f"  {csv_icon}  participants.csv — "
            f"{csv['total_subjects']} subject(s)"
        )
        for col in csv.get("required_columns") or []:
            missing = col.get("subjects_missing") or []
            invalid = col.get("subjects_invalid") or []
            ok = col["present"] and not missing and not invalid
            col_icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
            note = ""
            if not col["present"]:
                note = " [red](column absent)[/red]"
            else:
                parts = []
                if missing:
                    parts.append(f"{len(missing)} subject(s) empty")
                if invalid:
                    parts.append(f"{len(invalid)} subject(s) invalid value")
                if parts:
                    note = f" [yellow]({', '.join(parts)})[/yellow]"
            console.print(f"      {col_icon}  {col['column']}{note}")

    # Subject count check (harmonized pipelines)
    sc = report.get("subject_count")
    if sc:
        if sc["satisfied"] and not sc["recommended_met"]:
            icon = "[yellow]⚠[/yellow]"
            note = (
                f"[yellow]{sc['actual']} subject(s) — meets minimum ({sc['required']})"
                f" but below recommended ({sc['recommended']}) for reliable harmonization[/yellow]"
            )
        elif sc["satisfied"]:
            icon = "[green]✓[/green]"
            note = f"{sc['actual']} subject(s) (min {sc['required']}, recommended {sc['recommended']})"
        else:
            icon = "[red]✗[/red]"
            note = (
                f"[red]{sc['actual']} subject(s) — below minimum {sc['required']} "
                f"required for harmonization[/red]"
            )
        console.print(f"  {icon}  Subject count — {note}")

    console.print()


# ── nichart provenance ────────────────────────────────────────────────────────

@app.command()
def provenance(
    project: str = typer.Argument(..., help="Project name."),
    dirty_only: bool = typer.Option(False, "--dirty-only", "-d", help="Show only dirty/missing entries."),
) -> None:
    """Verify that cached pipeline step outputs are not stale."""
    report = _api("GET", f"/projects/{project}/provenance")
    summary = report.get("summary", "no_provenance")
    entries = report.get("entries") or []

    _SUMMARY_STYLE = {
        "all_clean": ("green", "✓ All steps clean"),
        "some_dirty": ("red", "✗ Some steps are dirty or have missing inputs"),
        "no_provenance": ("dim", "— No completed steps found"),
    }
    style, label = _SUMMARY_STYLE.get(summary, ("white", summary))
    console.print(f"\nProject [bold]{project}[/bold]: [{style}]{label}[/{style}]\n")

    if not entries:
        return

    _OVERALL_ICON = {
        "clean": "[green]✓[/green]",
        "dirty": "[red]✗[/red]",
        "missing_inputs": "[red]✗[/red]",
        "unreadable": "[yellow]?[/yellow]",
    }
    _INPUT_ICON = {
        "clean": "[green]·[/green]",
        "modified": "[red]M[/red]",
        "missing": "[red]![/red]",
    }

    for entry in entries:
        overall = entry.get("overall", "unreadable")
        if dirty_only and overall == "clean":
            continue

        icon = _OVERALL_ICON.get(overall, "?")
        step = entry.get("step_id") or "?"
        ts = entry.get("generated_at", "")[:16].replace("T", " ")
        console.print(
            f"  {icon}  [bold]{entry.get('output_dir', '?')}[/bold]  "
            f"[dim]step:{step}  pipeline:{entry.get('pipeline_id','?')}  @ {ts}[/dim]"
        )

        if overall == "unreadable":
            console.print(f"       [yellow]{entry.get('error')}[/yellow]")
            continue

        for inp in entry.get("inputs") or []:
            inp_icon = _INPUT_ICON.get(inp["status"], "?")
            note = ""
            if inp["status"] == "modified":
                note = f" [red]({inp['modified_count']} file(s) changed)[/red]"
            elif inp["status"] == "missing":
                note = " [red](not found)[/red]"
            console.print(f"       {inp_icon} {inp['label']}: [dim]{inp['path']}[/dim]{note}")

    console.print()


# ── nichart jobs (live dashboard + subcommands) ───────────────────────────────

def _build_dashboard(runs: list[dict]) -> Table:
    table = Table(
        box=box.SIMPLE_HEAVY,
        title="NiChart Jobs",
        title_style="bold",
    )
    table.add_column("Run ID", style="dim", width=10)
    table.add_column("Project", style="bold")
    table.add_column("Pipeline")
    table.add_column("Status", width=10)
    table.add_column("Step", justify="center")
    table.add_column("Elapsed", justify="right")
    table.add_column("Submitted")
    for r in runs:
        short_id = r["run_id"][:8]
        step_info = (
            f"{r['current_step'] + 1}/{r['total_steps']}"
            if r.get("total_steps") and r["status"] == "running"
            else "—"
        )
        table.add_row(
            short_id,
            r["project_id"],
            r["pipeline_id"],
            _status_text(r["status"]),
            step_info,
            _elapsed(r.get("submitted_at"), r.get("finished_at")),
            _fmt_dt(r.get("submitted_at")),
        )
    return table


def _build_detail(run: dict) -> Table:
    table = Table(
        box=box.SIMPLE_HEAVY,
        title=f"Run {run['run_id'][:8]}  [{run['pipeline_id']}]",
        title_style="bold",
    )
    table.add_column("Step", style="bold")
    table.add_column("Tool")
    table.add_column("Status", width=10)
    table.add_column("Elapsed", justify="right")
    table.add_column("Job ID", style="dim")
    for s in run.get("steps") or []:
        table.add_row(
            s["step_id"],
            s["tool_id"],
            _status_text(s["status"]),
            _elapsed(s.get("submitted_at"), s.get("finished_at")),
            s.get("job_id") or "—",
        )
    if run.get("error"):
        table.add_row("[red]error[/red]", "", Text(run["error"], style="red"), "", "")
    return table


def _is_terminal(status: str) -> bool:
    return status in ("succeeded", "failed", "cancelled")


@jobs_app.callback()
def jobs_cmd(
    ctx: typer.Context,
    run_id: Optional[str] = typer.Argument(default=None, help="Run ID to watch (omit for full dashboard)."),
    limit: int = typer.Option(20, "--limit", "-n", help="Max runs to show in dashboard mode."),
) -> None:
    """
    Show a live dashboard of all your runs, or watch a specific run.

    Pass a run ID to see per-step progress for that run.
    Polls until all shown runs are terminal (or press Ctrl+C to exit).
    """
    if ctx.invoked_subcommand is not None:
        return

    if run_id:
        _watch_run(run_id)
    else:
        _watch_dashboard(limit)


def _watch_dashboard(limit: int) -> None:
    console.print("[dim]Press Ctrl+C to exit.[/dim]\n")
    try:
        with Live(console=console, refresh_per_second=0.5) as live:
            while True:
                runs = _api("GET", f"/jobs/pipelines?limit={limit}")
                live.update(_build_dashboard(runs))
                if runs and all(_is_terminal(r["status"]) for r in runs):
                    break
                time.sleep(4)
    except KeyboardInterrupt:
        pass


def _watch_run(run_id: str) -> None:
    # Accept short prefix — find matching full run_id from the list if needed.
    console.print("[dim]Press Ctrl+C to exit.[/dim]\n")
    try:
        with Live(console=console, refresh_per_second=0.5) as live:
            while True:
                run = _api("GET", f"/jobs/pipelines/{run_id}")
                live.update(_build_detail(run))
                if _is_terminal(run["status"]):
                    break
                time.sleep(4)
    except KeyboardInterrupt:
        pass
    # Print final status line after Live exits
    run = _api("GET", f"/jobs/pipelines/{run_id}", silent_errors=True)
    if isinstance(run, dict):
        console.print(f"\nRun {run_id[:8]}: {_status_text(run['status'])}")


@jobs_app.command("submit")
def jobs_submit(
    project: str = typer.Argument(..., help="Project name."),
    pipeline_id: str = typer.Argument(..., help="Pipeline ID."),
    param: list[str] = typer.Option(
        [],
        "--param", "-p",
        help="Pipeline parameter as key=value (repeatable).",
    ),
    reuse_cache: bool = typer.Option(True, "--reuse-cache/--no-reuse-cache", help="Skip cached steps."),
    no_wait: bool = typer.Option(False, "--no-wait", help="Print run ID and exit immediately."),
    skip_readiness: bool = typer.Option(False, "--skip-readiness", help="Skip readiness check."),
) -> None:
    """Submit a pipeline job, then watch it live (use --no-wait to just get the run ID)."""
    # Parse --param key=value
    params: dict = {}
    for p in param:
        if "=" not in p:
            console.print(f"[red]Bad --param {p!r}[/red] — expected key=value")
            raise typer.Exit(1)
        k, _, v = p.partition("=")
        # Best-effort type coercion: int → float → bool → str
        for cast in (int, float):
            try:
                v = cast(v)
                break
            except ValueError:
                pass
        else:
            if v.lower() in ("true", "false"):
                v = v.lower() == "true"
        params[k.strip()] = v

    # Readiness check
    if not skip_readiness:
        report = _api("GET", f"/projects/{project}/readiness/{pipeline_id}", silent_errors=True)
        if isinstance(report, dict) and not report.get("satisfied", True):
            console.print("[yellow]Project is not ready to run this pipeline:[/yellow]")
            for img in report.get("imaging") or []:
                if not img["satisfied"]:
                    console.print(f"  [red]✗[/red]  Missing {img['modality'].upper()} imaging data")
            csv = report.get("csv")
            if csv and not csv["satisfied"]:
                console.print("  [red]✗[/red]  participants.csv incomplete")
            sc = report.get("subject_count")
            if sc and not sc["satisfied"]:
                console.print(
                    f"  [red]✗[/red]  Only {sc['actual']} subject(s); "
                    f"minimum {sc['required']} required for harmonization"
                )
            if not Confirm.ask("Submit anyway?", default=False):
                raise typer.Exit(0)
        elif isinstance(report, dict):
            sc = report.get("subject_count")
            if sc and not sc.get("recommended_met", True):
                console.print(
                    f"[yellow]⚠  Harmonization works best with {sc['recommended']}+ subjects; "
                    f"you have {sc['actual']}.[/yellow]"
                )

    run = _api(
        "POST",
        f"/projects/{project}/jobs/pipelines",
        json={
            "pipeline_id": pipeline_id,
            "params": params,
            "reuse_cached_steps": reuse_cache,
        },
    )
    run_id: str = run["run_id"]
    console.print(f"[green]Submitted[/green] run [bold]{run_id[:8]}[/bold]  (full ID: {run_id})")

    if no_wait:
        return

    console.print("[dim]Watching… Ctrl+C to detach.[/dim]\n")
    _watch_run(run_id)


@jobs_app.command("cancel")
def jobs_cancel(
    run_id: str = typer.Argument(..., help="Run ID to cancel."),
) -> None:
    """Cancel a running pipeline job."""
    _api("DELETE", f"/jobs/pipelines/{run_id}")
    console.print(f"[yellow]Cancellation requested[/yellow] for {run_id[:8]}")


@jobs_app.command("logs")
def jobs_logs(
    run_id: str = typer.Argument(..., help="Run ID."),
) -> None:
    """Print aggregated logs for a run."""
    result = _api("GET", f"/jobs/pipelines/{run_id}/logs")
    logs = result.get("logs", "")
    if logs:
        console.print(logs)
    else:
        console.print("[dim]No logs yet.[/dim]")


# ── Convenience top-level aliases ─────────────────────────────────────────────

@app.command("submit")
def submit_alias(
    project: str = typer.Argument(..., help="Project name."),
    pipeline_id: str = typer.Argument(..., help="Pipeline ID."),
    param: list[str] = typer.Option([], "--param", "-p", help="key=value parameter (repeatable)."),
    reuse_cache: bool = typer.Option(True, "--reuse-cache/--no-reuse-cache"),
    no_wait: bool = typer.Option(False, "--no-wait"),
    skip_readiness: bool = typer.Option(False, "--skip-readiness"),
) -> None:
    """Shorthand for: nichart jobs submit <project> <pipeline>."""
    jobs_submit(project, pipeline_id, param, reuse_cache, no_wait, skip_readiness)


@app.command("watch")
def watch_alias(
    run_id: str = typer.Argument(..., help="Run ID to watch."),
) -> None:
    """Shorthand for: nichart jobs <run_id>."""
    _watch_run(run_id)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app()


if __name__ == "__main__":
    main()
