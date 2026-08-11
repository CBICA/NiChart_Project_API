"""
NiChart CLI — terminal client for the NiChart API.

Typical usage
-------------
    nichart status                        # server health
    nichart cloud                         # cloud queue status (cloud mode)

    nichart projects create myproject
    nichart pipelines list
    nichart pipelines show dummy_pipeline
    nichart tools list

    nichart files upload-nifti myproject scan_T1.nii.gz
    nichart files upload-csv   myproject participants.csv
    nichart participants show  myproject

    nichart readiness myproject dummy_pipeline
    nichart jobs submit myproject dummy_pipeline --param duration_seconds=5
    nichart jobs                          # live dashboard of all your jobs
    nichart jobs <run_id>                 # live detail view for one run
    nichart jobs logs <run_id>
    nichart results show myproject dummy_pipeline

    nichart retention show    myproject   # when does this project expire? (cloud)
    nichart retention refresh myproject

All-in-one — create a project, upload data, verify, and run in one shot:

    nichart run run_dlmuse --project study1 --t1 /data/t1 --participants demo.csv
    nichart run run_spare_all --project study1 --t1 /data/t1 --fl /data/flair \\
                --participants demo.csv --wait-until-done

Server URL is read from the NICHART_API_URL environment variable
(default: http://localhost:8000).  Override per-command with --url.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
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

from app import modalities

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
tools_app        = typer.Typer(no_args_is_help=True, help="Browse available tools.")
results_app      = typer.Typer(no_args_is_help=True, help="Inspect pipeline results for a project.")
participants_app = typer.Typer(no_args_is_help=True, help="View the participants table.")
retention_app    = typer.Typer(no_args_is_help=True, help="View or refresh a project's retention timer (cloud mode).")

app.add_typer(projects_app, name="projects")
app.add_typer(files_app,    name="files")
app.add_typer(pipelines_app, name="pipelines")
app.add_typer(tools_app,    name="tools")
app.add_typer(jobs_app,     name="jobs")
app.add_typer(results_app,  name="results")
app.add_typer(participants_app, name="participants")
app.add_typer(retention_app, name="retention")

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
    # 204 No Content and other empty-bodied successes (deletes, CSV upload, …)
    # have no JSON to parse. Callers that need a body use JSON endpoints.
    if r.status_code == 204 or not r.content:
        return {}
    try:
        return r.json()
    except Exception:
        return {}


def _api_download(path: str) -> httpx.Response:
    try:
        return httpx.stream("GET", f"{_api_url}{path}", timeout=120, follow_redirects=True)
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {_api_url}[/red]")
        raise typer.Exit(1)


# ── Managed server sessions ───────────────────────────────────────────────────
#
# Some commands (notably `run`) can operate against a server they start
# themselves. The governing invariant is OWNERSHIP: we only ever shut down a
# server this process spawned; an already-running server is attached to and left
# untouched.
#
# Strategies (see `api_session`):
#   attach — require an existing server at the target URL (the classic behavior)
#   spawn  — always start a fresh ephemeral local server (local execution mode)
#   auto   — attach if one is reachable, otherwise spawn
#   remote — [NOT IMPLEMENTED] provision + start the server on a remote host over
#            SSH and tunnel to it (the "VS Code remote" model). Its lifecycle
#            contract is identical to `spawn` (owned server → torn down on exit);
#            only `_open_remote` below needs filling in. See CLI_run.md.


@dataclass
class ApiConnection:
    """A resolved API endpoint plus the lifecycle we owe it."""
    base_url: str
    owned: bool                              # True if this process started the server
    proc: Optional[subprocess.Popen] = None
    log_path: Optional[str] = None


def _probe(url: str) -> bool:
    """True if a NiChart API server answers /health at ``url`` (and looks like one)."""
    try:
        r = httpx.get(f"{url}/health", timeout=1.5)
    except Exception:
        return False
    if not r.is_success:
        return False
    try:
        body = r.json()
    except Exception:
        return False
    return isinstance(body, dict) and "execution_mode" in body


def _is_loopback(url: str) -> bool:
    return urllib.parse.urlparse(url).hostname in ("localhost", "127.0.0.1", "::1")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _print_log_tail(path: str, n: int = 20) -> None:
    try:
        lines = Path(path).read_text(errors="replace").splitlines()[-n:]
    except Exception:
        return
    if lines:
        console.print(f"[dim]— last {len(lines)} line(s) of {path} —[/dim]")
        for ln in lines:
            console.print(f"[dim]{ln}[/dim]")


def _graceful_shutdown(conn: ApiConnection) -> None:
    """SIGTERM the owned server, then SIGKILL if it doesn't exit. No-op if not owned."""
    if not conn.owned or conn.proc is None or conn.proc.poll() is not None:
        return
    conn.proc.terminate()
    try:
        conn.proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        conn.proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            conn.proc.wait(timeout=5)


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser for a .env file (ignores comments/blank lines)."""
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def _effective_spawn_backend(env_cfg: dict[str, str]) -> str:
    """The backend a spawned (forced-local) server would use, from .env + environment.

    os.environ overrides .env (matching pydantic-settings precedence). Because we
    force execution_mode=local, batch is only reached via an explicit override.
    """
    def val(name: str) -> Optional[str]:
        return os.environ.get(name) or env_cfg.get(name)

    return val("NICHART_JOB_BACKEND") or ("singularity" if val("NICHART_SIF_DIR") else "docker")


def _spawn_local(log_path: Optional[str], inactivity_timeout: int = 0) -> ApiConnection:
    """Start an ephemeral local-mode API server subprocess; wait until it's healthy.

    The server runs with cwd = the API repo root so it loads the operator's
    ``.env`` and finds the relative ``resources/`` directory. Config precedence:
    inherited environment > repo ``.env`` > server defaults, with
    execution_mode forced to local (so the CLI can talk to it without auth).
    """
    # Merge config from the same locations the server resolves (INSTALLATION.md §4),
    # lowest → highest precedence; used below to pick the backend / warn sensibly.
    _candidates = [ENV_FILE, Path.home() / ".nichart" / ".env"]
    _explicit = os.environ.get("NICHART_ENV_FILE")
    if _explicit:
        _candidates.append(Path(_explicit))
    env_cfg: dict[str, str] = {}
    found_config = False
    for cand in _candidates:
        if cand.exists():
            env_cfg.update(_read_env_file(cand))  # later candidate overrides earlier
            found_config = True
    if not found_config and not any(k.startswith("NICHART_") for k in os.environ):
        console.print(
            "[yellow]No configuration found[/yellow] (no .env at the repo, ~/.nichart/.env, "
            "or $NICHART_ENV_FILE, and no NICHART_* env vars) — the spawned server will use "
            "defaults. See INSTALLATION.md §4."
        )

    backend = _effective_spawn_backend(env_cfg)
    if backend == "batch":
        console.print(
            "[red]Your API config selects the AWS Batch backend (NICHART_JOB_BACKEND=batch).[/red]\n"
            "That runs on the cloud and can't be a locally-spawned server. Point --url at your "
            "deployed API and use --server attach."
        )
        raise typer.Exit(1)
    if backend == "docker" and shutil.which("docker") is None and not Path("/var/run/docker.sock").exists():
        console.print(
            "[yellow]Warning:[/yellow] Docker backend selected but no Docker found "
            "(no `docker` on PATH, no docker socket). Pipeline steps will fail without it."
        )

    port = _free_loopback_port()
    url = f"http://127.0.0.1:{port}"

    if log_path:
        log_handle: object = open(log_path, "wb")
        log_name = log_path
    else:
        tmp = tempfile.NamedTemporaryFile(prefix="nichart-server-", suffix=".log", delete=False)
        log_handle, log_name = tmp, tmp.name

    env = dict(os.environ)
    env["NICHART_EXECUTION_MODE"] = "local"
    # Courtesy on shared systems: an ephemeral server self-terminates after a
    # period of inactivity (with no active runs), so an orphaned spawn (e.g. the
    # CLI was killed before teardown) doesn't linger. 0 disables.
    env["NICHART_INACTIVITY_TIMEOUT_SECONDS"] = str(inactivity_timeout)
    cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]
    try:
        proc = subprocess.Popen(
            cmd, stdout=log_handle, stderr=subprocess.STDOUT, env=env, cwd=str(REPO_ROOT),  # type: ignore[arg-type]
        )
    except Exception as exc:
        console.print(f"[red]Failed to launch local server:[/red] {exc}")
        raise typer.Exit(1)

    conn = ApiConnection(base_url=url, owned=True, proc=proc, log_path=log_name)
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            console.print(f"[red]Local server exited during startup (code {proc.returncode}).[/red]")
            _print_log_tail(log_name)
            raise typer.Exit(1)
        if _probe(url):
            return conn
        time.sleep(0.3)

    console.print("[red]Local server did not become healthy within 30s.[/red]")
    _print_log_tail(log_name)
    _graceful_shutdown(conn)
    raise typer.Exit(1)


def _open_remote(url_hint: str, log_path: Optional[str]) -> ApiConnection:
    """[STUB] Provision + start the server on a remote host and tunnel to it.

    Planned "VS Code remote" model: over SSH, ensure the NiChart server package or
    container is installed on the host (the "install server component" step), start
    it bound to loopback there, open an SSH tunnel to a local port, and return an
    owned ApiConnection pointing at the tunnel. Teardown then stops the remote
    server and closes the tunnel — identical ownership contract to a local spawn.
    Not implemented yet.
    """
    console.print("[red]The 'remote' server strategy is not implemented yet.[/red]")
    raise typer.Exit(1)


@contextlib.contextmanager
def api_session(url_hint: str, *, strategy: str = "auto", keep: bool = False,
                log_path: Optional[str] = None, inactivity_timeout: int = 0):
    """Yield an :class:`ApiConnection`, managing the server lifecycle when we own it.

    While the context is active the module-level API URL is pointed at the
    connection, so plain ``_api``/``_api_download`` calls target it. Only a server
    this process started is shut down on exit (unless ``keep``).
    """
    global _api_url

    if strategy == "remote":
        conn = _open_remote(url_hint, log_path)
    elif strategy in ("auto", "attach") and _probe(url_hint):
        conn = ApiConnection(base_url=url_hint, owned=False)
    elif strategy == "attach":
        console.print(
            f"[red]No API server reachable at {url_hint}.[/red] "
            "Start one, or use --server auto/spawn."
        )
        raise typer.Exit(1)
    else:
        # strategy == "spawn", or "auto" with nothing running → spawn locally.
        if not _is_loopback(url_hint):
            console.print(
                f"[red]Cannot auto-start a server for a remote target ({url_hint}).[/red] "
                "Start the server there and point --url at it, or use --server attach."
            )
            raise typer.Exit(1)
        conn = _spawn_local(log_path, inactivity_timeout=inactivity_timeout)
        _idle = (f", idle auto-shutdown after {inactivity_timeout}s"
                 if inactivity_timeout and inactivity_timeout > 0 else "")
        console.print(
            f"[dim]Started a local API server (pid {conn.proc.pid}) at {conn.base_url}{_idle}[/dim]"
        )

    saved_url = _api_url
    _api_url = conn.base_url
    try:
        yield conn
    finally:
        _api_url = saved_url
        if conn.owned and not keep:
            console.print("[dim]Shutting down the local API server…[/dim]")
            _graceful_shutdown(conn)
        elif conn.owned and keep and conn.proc is not None:
            console.print(
                f"[dim]Leaving local API server running (pid {conn.proc.pid}) at "
                f"{conn.base_url} — stop it yourself (--keep-server).[/dim]"
            )


# ── Formatting helpers ─────────────────────────────────────────────────────────

_STATUS_STYLE = {
    "pending":   "yellow",
    "running":   "cyan",
    "succeeded": "green",
    "failed":    "red",
    "skipped":   "dim",
    "cancelled": "dim",
}

VALID_MODALITIES = modalities.MODALITY_CODES

# Repo root = parent of app/. Used to locate the server's .env and resources/ when
# spawning a managed server, so it runs with the config the operator set up at
# install time — not whatever directory the user happens to invoke `nichart` from.
# (pydantic-settings loads .env from cwd, and NICHART_RESOURCES_PATH defaults to a
# relative "resources", so cwd must be the repo root for the server to work.)
REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"

# Default idle auto-shutdown (seconds) for a CLI-spawned server — a courtesy on
# shared systems so an orphaned ephemeral server doesn't linger. 0 disables.
_DEFAULT_SPAWN_INACTIVITY = 1800


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


def _parse_params(param: list[str]) -> dict:
    """Parse repeated ``--param key=value`` into a dict with best-effort typing.

    Values are coerced int → float → bool → str (first that fits). Raises with a
    clear message on a malformed entry.
    """
    params: dict = {}
    for p in param:
        if "=" not in p:
            console.print(f"[red]Bad --param {p!r}[/red] — expected key=value")
            raise typer.Exit(1)
        k, _, raw = p.partition("=")
        val: object = raw
        for cast in (int, float):
            try:
                val = cast(raw)
                break
            except ValueError:
                pass
        else:
            if raw.lower() in ("true", "false"):
                val = raw.lower() == "true"
        params[k.strip()] = val
    return params


def _gather_niftis(path: Path) -> list[Path]:
    """Resolve a path to NIfTI files: a directory yields all NIfTIs within (flat),
    a single file yields itself. Errors clearly if nothing usable is found."""
    if not path.exists():
        console.print(f"[red]Path not found:[/red] {path}")
        raise typer.Exit(1)
    if path.is_dir():
        files = sorted(
            p for p in path.iterdir()
            if p.is_file() and (p.name.endswith(".nii") or p.name.endswith(".nii.gz"))
        )
        if not files:
            console.print(f"[red]No NIfTI files (.nii / .nii.gz) found in[/red] {path}")
            raise typer.Exit(1)
        return files
    if not (path.name.endswith(".nii") or path.name.endswith(".nii.gz")):
        console.print(f"[red]Not a NIfTI file:[/red] {path}")
        raise typer.Exit(1)
    return [path]


def _upload_modality(project: str, modality: str, files: list[Path]) -> list[str]:
    """Non-interactively upload NIfTIs as a fixed modality; returns committed MRIDs.

    The MRID for each file comes from server-side filename inference. A file whose
    MRID can't be inferred is a hard error (the whole staged batch is discarded and
    the offending files are listed), since committing without an MRID is unsafe.
    """
    upload_files = [
        ("files", (f.name, f.open("rb"), "application/octet-stream")) for f in files
    ]
    try:
        resp = _api("POST", f"/projects/{project}/files/upload/nifti", files=upload_files)
    finally:
        for _, (_, fh, _) in upload_files:
            fh.close()

    staging_id = resp["staging_id"]
    proposals = resp["proposals"]
    missing = [p["filename"] for p in proposals if not p.get("inferred_mrid")]
    if missing:
        _api("DELETE", f"/projects/{project}/files/stage/{staging_id}", silent_errors=True)
        console.print(
            f"[red]Could not infer an MRID for {len(missing)} {modality.upper()} file(s):[/red]"
        )
        for m in missing:
            console.print(f"    {m}")
        console.print(
            "[dim]Rename so the subject ID is derivable, or upload these individually "
            "with `nichart files upload-nifti` to set MRIDs by hand.[/dim]"
        )
        raise typer.Exit(1)

    mappings = [
        {"filename": p["filename"], "mrid": p["inferred_mrid"], "modality": modality}
        for p in proposals
    ]
    result = _api(
        "POST", f"/projects/{project}/files/stage/{staging_id}/commit",
        json={"mappings": mappings},
    )
    return [c["mrid"] for c in result.get("committed", [])]


def _render_readiness(project: str, pipeline_id: str, report: dict) -> bool:
    """Render a readiness report; return whether it is satisfied."""
    satisfied = report.get("satisfied", False)
    badge = Text("READY", style="green") if satisfied else Text("NOT READY", style="red")
    console.print(f"\nProject [bold]{project}[/bold] → pipeline [bold]{pipeline_id}[/bold]: ", end="")
    console.print(badge)
    console.print()

    for img in report.get("imaging") or []:
        icon = "[green]✓[/green]" if img["satisfied"] else "[red]✗[/red]"
        console.print(f"  {icon}  {img['modality'].upper()} imaging — {img['subject_count']} subject(s)")

    csv = report.get("csv")
    if csv:
        csv_icon = "[green]✓[/green]" if csv["satisfied"] else "[red]✗[/red]"
        console.print(f"  {csv_icon}  participants.csv — {csv['total_subjects']} subject(s)")
        for col in csv.get("required_columns") or []:
            missing = col.get("subjects_missing") or []
            invalid = col.get("subjects_invalid") or []
            ok = col["present"] and not missing and not invalid
            col_icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
            if not col["present"]:
                note = " [red](column absent)[/red]"
            else:
                parts = []
                if missing:
                    parts.append(f"{len(missing)} subject(s) empty")
                if invalid:
                    parts.append(f"{len(invalid)} subject(s) invalid value")
                note = f" [yellow]({', '.join(parts)})[/yellow]" if parts else ""
            console.print(f"      {col_icon}  {col['column']}{note}")

    sc = report.get("subject_count")
    if sc:
        if sc["satisfied"] and not sc["recommended_met"]:
            icon, note = "[yellow]⚠[/yellow]", (
                f"[yellow]{sc['actual']} subject(s) — meets minimum ({sc['required']}) "
                f"but below recommended ({sc['recommended']}) for reliable harmonization[/yellow]"
            )
        elif sc["satisfied"]:
            icon, note = "[green]✓[/green]", (
                f"{sc['actual']} subject(s) (min {sc['required']}, recommended {sc['recommended']})"
            )
        else:
            icon, note = "[red]✗[/red]", (
                f"[red]{sc['actual']} subject(s) — below minimum {sc['required']} "
                f"required for harmonization[/red]"
            )
        console.print(f"  {icon}  Subject count — {note}")

    console.print()
    return satisfied


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
    _render_readiness(project, pipeline_id, report)


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
    params = _parse_params(param)

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


# ── nichart cloud ─────────────────────────────────────────────────────────────

def _fmt_seconds(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


@app.command("cloud")
def cloud() -> None:
    """Show cloud execution/queue status: running + pending jobs and drain estimate.

    In local mode this reports mode=local with no queue data.
    """
    data = _api("GET", "/cloud/status")
    table = Table(show_header=False, box=box.SIMPLE)
    table.add_row("Mode", data.get("mode", "—"))
    table.add_row("Queue", data.get("queue_name") or "—")
    rj = data.get("running_job_count")
    pj = data.get("pending_job_count")
    table.add_row("Running jobs", str(rj) if rj is not None else "—")
    table.add_row("Pending jobs", str(pj) if pj is not None else "—")
    table.add_row("Est. queue drain", _fmt_seconds(data.get("estimated_queue_drain_seconds")))
    console.print(table)


# ── nichart tools ─────────────────────────────────────────────────────────────

@tools_app.command("list")
def tools_list() -> None:
    """List all available tools."""
    items = _api("GET", "/catalog/tools")
    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("ID", style="bold")
    table.add_column("Name")
    table.add_column("Description")
    for t in items:
        table.add_row(t["id"], t.get("name", ""), (t.get("description") or "")[:70])
    console.print(table)


@tools_app.command("show")
def tools_show(
    tool_id: str = typer.Argument(..., help="Tool ID (see: nichart tools list)."),
) -> None:
    """Show a tool's inputs, outputs, parameters, and resource requirements."""
    t = _api("GET", f"/catalog/tools/{tool_id}")
    console.print(f"\n[bold]{t.get('name', tool_id)}[/bold]  [dim]({t['id']})[/dim]")
    if t.get("description"):
        console.print(t["description"])
    for section in ("inputs", "outputs"):
        d = t.get(section) or {}
        if d:
            console.print(f"\n[underline]{section.title()}[/underline]")
            for label, spec in d.items():
                console.print(f"  {label}: [dim]{spec.get('type', '')}[/dim]")
    res = t.get("resources") or {}
    if res:
        console.print(
            f"\n[underline]Resources[/underline]  "
            f"vcpus={res.get('vcpus')}  memory={res.get('memory')} MiB  gpus={res.get('gpus', 0)}"
        )
    params = t.get("parameters") or {}
    if params:
        console.print("\n[underline]Parameters[/underline]")
        for name, spec in params.items():
            console.print(f"  {name}: [dim]{spec.get('type', '')}  default={spec.get('default')}[/dim]")
    console.print()


# ── nichart results ───────────────────────────────────────────────────────────

@results_app.command("list")
def results_list(
    project: str = typer.Argument(..., help="Project name."),
) -> None:
    """List pipelines that have results in this project."""
    items = _api("GET", f"/projects/{project}/results")
    if not items:
        console.print("[dim]No results yet.[/dim]")
        return
    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("Pipeline", style="bold")
    table.add_column("Name")
    table.add_column("Batch features", justify="center")
    table.add_column("Per-subject", justify="right")
    table.add_column("Atlas", justify="center")
    for r in items:
        table.add_row(
            r["pipeline_id"],
            r.get("pipeline_name", ""),
            "✓" if r.get("has_batch_features") else "—",
            str(len(r.get("per_subject_ids") or [])),
            "✓" if r.get("has_atlas") else "—",
        )
    console.print(table)


@results_app.command("show")
def results_show(
    project: str = typer.Argument(..., help="Project name."),
    pipeline_id: str = typer.Argument(..., help="Pipeline ID."),
) -> None:
    """Show detailed results for one pipeline: features, per-subject outputs, completeness."""
    r = _api("GET", f"/projects/{project}/results/{pipeline_id}")
    console.print(f"\n[bold]{r.get('pipeline_name', pipeline_id)}[/bold]  [dim]({pipeline_id})[/dim]")

    bf = r.get("batch_features")
    if bf:
        avail = "[green]available[/green]" if bf.get("available") else "[red]missing[/red]"
        console.print(f"\n[underline]Batch features[/underline] — {avail}")
        if bf.get("available"):
            console.print(f"  {bf.get('row_count', 0)} row(s), {len(bf.get('columns') or [])} column(s)")
            if bf.get("download_path"):
                console.print(
                    f"  [dim]download:[/dim] nichart files download {project} {bf['download_path']}"
                )

    ps = r.get("per_subject") or []
    if ps:
        console.print("\n[underline]Per-subject outputs[/underline]")
        for o in ps:
            subs = o.get("subjects") or {}
            have = sum(1 for v in subs.values() if v.get("available"))
            console.print(
                f"  {o.get('display_name') or o.get('id')} [dim]({o.get('type')})[/dim] — "
                f"{have}/{len(subs)} subject(s)"
            )

    subj = r.get("subjects") or {}
    if subj:
        complete = sum(1 for v in subj.values() if v.get("complete"))
        console.print(f"\n[underline]Completeness[/underline] — {complete}/{len(subj)} subject(s) complete")
    console.print()


# ── nichart participants ──────────────────────────────────────────────────────

@participants_app.command("show")
def participants_show(
    project: str = typer.Argument(..., help="Project name."),
) -> None:
    """Show the participants table."""
    data = _api("GET", f"/projects/{project}/participants")
    rows = data.get("rows") or []
    if not rows:
        console.print("[dim]No participants.csv, or it is empty.[/dim]")
        return
    cols: list[str] = []
    for row in rows:
        for k in row:
            if k not in cols:
                cols.append(k)
    table = Table(box=box.SIMPLE_HEAVY)
    for c in cols:
        table.add_column(c, style="bold" if c.upper() == "MRID" else None)
    for row in rows[:200]:
        table.add_row(*[str(row.get(c, "")) for c in cols])
    console.print(table)
    suffix = " (showing first 200)" if len(rows) > 200 else ""
    console.print(f"[dim]{len(rows)} row(s){suffix}[/dim]")


@participants_app.command("template")
def participants_template(
    project: str = typer.Argument(..., help="Project name."),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="Output path (default: participants_template.csv)."),
) -> None:
    """Download a participants CSV template pre-filled with detected MRIDs."""
    dest = Path(out) if out else Path("participants_template.csv")
    with _api_download(f"/projects/{project}/participants/template") as resp:
        if not resp.is_success:
            console.print(f"[red]Error {resp.status_code}[/red]")
            raise typer.Exit(1)
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=65536):
                f.write(chunk)
    console.print(f"[green]Saved[/green] {dest}")


# ── nichart retention ─────────────────────────────────────────────────────────

@retention_app.command("show")
def retention_show(
    project: str = typer.Argument(..., help="Project name."),
) -> None:
    """Show when a project is scheduled to expire (cloud mode only)."""
    data = _api("GET", f"/projects/{project}/retention")
    exp = data.get("expires_at")
    console.print(f"\nProject [bold]{project}[/bold] expires at: [bold]{_fmt_dt(exp)}[/bold]")
    if exp:
        remaining = datetime.fromisoformat(exp.replace("Z", "+00:00")) - datetime.now(timezone.utc)
        secs = int(remaining.total_seconds())
        if secs <= 0:
            console.print("[red]Already past expiry — eligible for deletion.[/red]")
        else:
            console.print(
                f"[dim]{secs // 86400}d {(secs % 86400) // 3600}h remaining[/dim]  — "
                f"refresh with: nichart retention refresh {project}"
            )
    console.print()


@retention_app.command("refresh")
def retention_refresh(
    project: str = typer.Argument(..., help="Project name."),
) -> None:
    """Reset a project's retention timer to the full window (cloud mode only)."""
    data = _api("POST", f"/projects/{project}/retention/refresh")
    console.print(f"[green]Refreshed.[/green] New expiry: [bold]{_fmt_dt(data.get('expires_at'))}[/bold]")


# ── nichart run (all-in-one) ──────────────────────────────────────────────────

@app.command("run")
def run(
    pipeline_id: str = typer.Argument(..., help="Pipeline to run (see: nichart pipelines list)."),
    project: str = typer.Option(..., "--project", "-P", help="Project name. Created new unless --existing is given."),
    t1: Optional[Path] = typer.Option(None, "--t1", help="T1-weighted NIfTIs: a flat directory (all NIfTIs within) or a single .nii/.nii.gz."),
    fl: Optional[Path] = typer.Option(None, "--fl", "--flair", help="FLAIR NIfTIs (directory or file)."),
    t2: Optional[Path] = typer.Option(None, "--t2", help="T2 NIfTIs (directory or file)."),
    t1ce: Optional[Path] = typer.Option(None, "--t1ce", help="T1CE NIfTIs (directory or file)."),
    adc: Optional[Path] = typer.Option(None, "--adc", help="ADC NIfTIs (directory or file)."),
    pet: Optional[Path] = typer.Option(None, "--pet", help="PET NIfTIs (directory or file)."),
    image: list[str] = typer.Option(
        [], "--image",
        help=(
            "Fallback for a modality without a dedicated flag above: MODALITY=PATH "
            "(repeatable). Prefer the named flags when one exists. Valid modalities: "
            + ", ".join(modalities.MODALITY_CODES) + "."
        ),
    ),
    participants: Optional[Path] = typer.Option(None, "--participants", help="Participants CSV (subject IDs + covariates)."),
    existing: bool = typer.Option(False, "--existing", help="Add to an existing project instead of creating a new one."),
    param: list[str] = typer.Option([], "--param", "-p", help="Pipeline parameter as key=value (repeatable)."),
    reuse_cache: bool = typer.Option(True, "--reuse-cache/--no-reuse-cache", help="Reuse cached step outputs when inputs are unchanged."),
    wait: bool = typer.Option(False, "--wait-until-done/--no-wait", help="Block and stream live progress until the run finishes."),
    force: bool = typer.Option(False, "--force", help="Submit even if the readiness check fails."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate inputs and print the plan without creating, uploading, or submitting."),
    server: str = typer.Option("auto", "--server", help="Server strategy: 'auto' (attach if one is running, else start a local one), 'attach' (require a running server), or 'spawn' (always start a fresh local one)."),
    keep_server: bool = typer.Option(False, "--keep-server", help="Don't shut down a server this command started (leave it running)."),
    server_log: Optional[str] = typer.Option(None, "--server-log", help="File to write a spawned server's logs to (default: a temp file)."),
    server_timeout: Optional[int] = typer.Option(None, "--server-timeout", help="Idle auto-shutdown (seconds) for a server this command spawns; the timer never fires while a run is in progress. Default: 1800 (30 min), or disabled with --keep-server. Use -1/0 to disable."),
) -> None:
    """
    Run a pipeline end-to-end in one command.

    Performs, in order — each step verified before the next:

      1. Verify the pipeline exists.
      2. Create the project (or select it with --existing; collisions are errors).
      3. Upload each provided modality (--t1 / --fl / --t2 / --t1ce / --adc /
         --pet, or --image MOD=PATH for any other) as NIfTI. Each flag takes a
         flat directory (every NIfTI within) or a single file; MRIDs are inferred
         from filenames.
      4. Upload the participants CSV (--participants), if given.
      5. Report per-modality subject counts and flag any mismatches.
      6. Check pipeline readiness (imaging, CSV columns, subject count).
      7. Submit the pipeline.
      8. Print how to track the run — or, with --wait-until-done, stream live
         progress until it finishes.

    Server: by default (--server auto) this attaches to a running API server if
    one is reachable, otherwise it starts a local one, runs, waits for the run to
    finish, then shuts that server down. Because a spawned local server also runs
    the pipeline, --no-wait is ignored when we started the server (you'll be told).
    Use --server attach to require an already-running server, or --server spawn to
    always start a fresh local one. Remote servers are not yet supported (see
    CLI_run.md).

    Use --dry-run to validate inputs and preview the plan without starting a
    server or touching data. If a newly-created project's setup fails partway, the
    command tells you the project was left with partial data and how to remove it.

    Examples:
      nichart run run_dlmuse --project study1 --t1 /data/t1
      nichart run run_spare_all --project study1 --t1 /data/t1 --fl /data/flair \\
                  --participants demo.csv --wait-until-done
    """
    # Modality inputs: the common named flags plus any --image MODALITY=PATH.
    provided: dict[str, Path] = {
        m: p for m, p in (("t1", t1), ("fl", fl), ("t2", t2), ("t1ce", t1ce), ("adc", adc), ("pet", pet)) if p is not None
    }
    for spec in image:
        if "=" not in spec:
            console.print(f"[red]Bad --image {spec!r}[/red] — expected MODALITY=PATH")
            raise typer.Exit(1)
        code, _, pth = spec.partition("=")
        code = code.strip().lower()
        if not modalities.is_valid(code):
            console.print(
                f"[red]Unknown modality {code!r}[/red] — valid: {', '.join(modalities.MODALITY_CODES)}"
            )
            raise typer.Exit(1)
        if code in provided:
            console.print(f"[red]Modality {code!r} given twice[/red] (named flag and --image).")
            raise typer.Exit(1)
        provided[code] = Path(pth).expanduser()

    params = _parse_params(param)

    # Pre-flight — purely local, no server needed. Resolve modality file lists and
    # validate the CSV path up front so problems surface before anything starts.
    resolved: dict[str, list[Path]] = {mod: _gather_niftis(path) for mod, path in provided.items()}
    if participants is not None and not participants.is_file():
        console.print(f"[red]Participants CSV not found:[/red] {participants}")
        raise typer.Exit(1)

    # Plan summary.
    console.print(f"\n[bold]Pipeline:[/bold] {pipeline_id}")
    console.print(f"[bold]Project: [/bold] {project}  [dim]({'existing' if existing else 'new'})[/dim]")
    for mod, files in resolved.items():
        console.print(f"  {mod.upper():5} {len(files):3} file(s)  [dim]{provided[mod]}[/dim]")
    if participants:
        console.print(f"  {'CSV':5}      [dim]{participants}[/dim]")
    if params:
        console.print(f"  [dim]params: {params}[/dim]")

    if dry_run:
        # Dry-run never starts a server. If one is already reachable, use it for
        # the pipeline/collision checks; otherwise validate locally and say so.
        console.print("\n[yellow]Dry run — nothing created, uploaded, or submitted.[/yellow]")
        if _probe(_api_url):
            if not isinstance(_api("GET", f"/catalog/pipelines/{pipeline_id}", silent_errors=True), dict):
                console.print(f"[red]Would fail:[/red] unknown pipeline '{pipeline_id}'.")
            known = _api("GET", "/projects", silent_errors=True)
            names = {p["id"] for p in known} if isinstance(known, list) else set()
            if not existing and project in names:
                console.print(f"[red]Would fail:[/red] project '{project}' already exists (use --existing).")
            if existing and project not in names:
                console.print(f"[red]Would fail:[/red] project '{project}' does not exist (drop --existing).")
        else:
            console.print(
                f"[dim]No server reachable at {_api_url}; pipeline/project checks skipped. "
                "The real run would start a local server (--server auto).[/dim]"
            )
        raise typer.Exit(0)

    # Resolve idle auto-shutdown for a spawned server. Unset → 30 min, unless
    # --keep-server (then off, since you asked to keep it). A negative value disables.
    if server_timeout is None:
        spawn_timeout = 0 if keep_server else _DEFAULT_SPAWN_INACTIVITY
    else:
        spawn_timeout = max(0, server_timeout)

    with api_session(_api_url, strategy=server, keep=keep_server, log_path=server_log,
                     inactivity_timeout=spawn_timeout) as conn:
        # 1. Pipeline must exist.
        pipe = _api("GET", f"/catalog/pipelines/{pipeline_id}", silent_errors=True)
        if not isinstance(pipe, dict):
            console.print(f"[red]Unknown pipeline:[/red] {pipeline_id}")
            console.print("[dim]See available pipelines with: nichart pipelines list[/dim]")
            raise typer.Exit(1)
        if pipe.get("name") and pipe["name"] != pipeline_id:
            console.print(f"[dim]→ {pipe['name']}[/dim]")

        known_projects = _api("GET", "/projects", silent_errors=True)
        names = {p["id"] for p in known_projects} if isinstance(known_projects, list) else set()

        created_new = False
        try:
            # 2. Project create / select.
            if existing:
                if project not in names:
                    console.print(f"[red]Project '{project}' does not exist.[/red] Drop --existing to create it.")
                    raise typer.Exit(1)
                console.print(f"\n[dim]Using existing project[/dim] [bold]{project}[/bold]")
            else:
                if project in names:
                    console.print(
                        f"[red]Project '{project}' already exists.[/red] "
                        "Pass --existing to add to it, or choose another name."
                    )
                    raise typer.Exit(1)
                _api("POST", "/projects", json={"name": project})
                created_new = True
                console.print(f"\n[green]Created[/green] project [bold]{project}[/bold]")

            # 3. Upload modalities.
            per_modality: dict[str, set[str]] = {}
            for mod, files in resolved.items():
                console.print(f"[dim]Uploading {len(files)} {mod.upper()} file(s)…[/dim]")
                mrids = _upload_modality(project, mod, files)
                per_modality[mod] = set(mrids)
                console.print(f"  [green]✓[/green] {mod.upper()}: {len(mrids)} subject(s)")

            # 4. Participants.
            if participants:
                _api(
                    "POST", f"/projects/{project}/files/upload/csv",
                    files={"file": (participants.name, participants.open("rb"), "text/csv")},
                )
                console.print(f"  [green]✓[/green] participants: {participants.name}")

            # 5. Cross-modality subject-count diagnostics.
            if len(per_modality) > 1:
                counts = {m: len(s) for m, s in per_modality.items()}
                if len(set(counts.values())) > 1:
                    console.print("\n[yellow]⚠ Subject counts differ across modalities:[/yellow]")
                    all_mrids = set().union(*per_modality.values())
                    for m, s in per_modality.items():
                        console.print(f"    {m.upper():5} {len(s)} subject(s)")
                    for m, s in per_modality.items():
                        miss = sorted(all_mrids - s)
                        if miss:
                            console.print(f"    [dim]{m.upper()} missing: {', '.join(miss)}[/dim]")

            # 6. Readiness.
            report = _api("GET", f"/projects/{project}/readiness/{pipeline_id}", silent_errors=True)
            if isinstance(report, dict):
                satisfied = _render_readiness(project, pipeline_id, report)
                if not satisfied and not force:
                    console.print(
                        "[red]Project is not ready to run this pipeline.[/red] "
                        "Fix the issues above, or re-run with --force to submit anyway."
                    )
                    raise typer.Exit(1)
            else:
                console.print("[yellow]Could not evaluate readiness; proceeding.[/yellow]")

            # 7. Submit.
            run_rec = _api(
                "POST", f"/projects/{project}/jobs/pipelines",
                json={"pipeline_id": pipeline_id, "params": params, "reuse_cached_steps": reuse_cache},
            )
            run_id: str = run_rec["run_id"]
            console.print(f"\n[green]Submitted[/green] run [bold]{run_id[:8]}[/bold]  [dim](full ID: {run_id})[/dim]")

        except typer.Exit as e:
            if created_new and e.exit_code != 0:
                console.print(
                    f"[dim]Project '{project}' was created but setup did not complete; it may hold partial data. "
                    f"Remove it with: nichart projects delete {project}[/dim]"
                )
            raise

        # 8. Wait, or hand back tracking commands. A server we started also runs
        # the pipeline, so we must block until it finishes before tearing it down —
        # this overrides --no-wait.
        if conn.owned and not wait:
            console.print(
                "[dim]This command started the local server, so it will wait for the run "
                "to finish before shutting it down (overrides --no-wait).[/dim]"
            )
        if wait or conn.owned:
            console.print("[dim]Watching… Ctrl+C detaches"
                          + ("; the managed server is then shut down and the run stops." if conn.owned
                             else " (the run keeps going).") + "[/dim]\n")
            _watch_run(run_id)
        else:
            console.print("[dim]Track it with:[/dim]")
            console.print(f"    nichart jobs {run_id}")
            console.print(f"    nichart jobs logs {run_id}")
            console.print(f"[dim]View results when done:[/dim] nichart results show {project} {pipeline_id}")


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
