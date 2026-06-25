"""
Pipeline run store and async orchestration background tasks.

Each submission creates a ``_RunRecord`` immediately and schedules an async
background task (via FastAPI's ``BackgroundTasks``) that drives step-by-step
execution. Clients poll the status/detail endpoints.

Persistence
-----------
Run records are written through to ``{study_dir}/_working/pipeline_runs.json``
after every state transition. On startup, ``load_runs_from_disk`` scans the
data root for these files and restores the in-memory store. Runs that were
in-progress at shutdown are marked as failed (the background task is gone).

Step caching
------------
Cache state is stored in ``{study_dir}/_working/metadata.json``, keyed by an
MD5 of ``tool_id|inputs|params``. A step is skipped when its cache entry shows
``status=success`` and no input path has been modified since ``finished_time``.
"""

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    _API_VERSION = importlib.metadata.version("nichart-api")
except importlib.metadata.PackageNotFoundError:
    _API_VERSION = "0.1.0"

from fastapi import HTTPException

from app.backends.base import JobBackend, JobHandle
from app.models.jobs import (
    PipelineRunDetail,
    PipelineRunLogs,
    PipelineRunSummary,
    StepStatus,
)
from app.services import catalog_service, s3_sync_service

_log = logging.getLogger(__name__)


# ── Run store ──────────────────────────────────────────────────────────────────

@dataclass
class _StepRecord:
    step_id: str
    tool_id: str
    status: str = "pending"
    submitted_at: datetime | None = None
    finished_at: datetime | None = None
    job_id: str | None = None
    log_path: str | None = None  # persistent log file path (SLURM only); used for reconnect
    container_image: str | None = None
    error: str | None = None
    logs: str = ""


@dataclass
class _RunRecord:
    run_id: str
    user_id: str
    project_id: str
    pipeline_id: str
    status: str = "pending"
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    current_step: int = 0
    total_steps: int = 0
    steps: list[_StepRecord] = field(default_factory=list)
    error: str | None = None
    cancelled: bool = False
    backend_type: str = ""  # e.g. "docker", "singularity", "slurm", "batch"


_runs: dict[str, _RunRecord] = {}

# Maps run_id → the JobHandle of the step currently executing.
# Populated in _poll_to_completion; used by get_run_logs for live log tailing.
_active_handles: dict[str, JobHandle] = {}


# ── Path resolution (pipeline YAML templates) ─────────────────────────────────

_STUDY_RE = re.compile(r"\$\{STUDY\}")
_STEP_REF_RE = re.compile(r"\$\{(\w+)\.outputs\.(\w+)\}")


def _resolve_path(
    template: str,
    study_dir: Path,
    step_outputs: dict[str, dict[str, str]],
) -> str:
    result = _STUDY_RE.sub(str(study_dir), template)
    for m in _STEP_REF_RE.finditer(result):
        step_id, label = m.group(1), m.group(2)
        val = step_outputs.get(step_id, {}).get(label, "")
        result = result.replace(m.group(0), val)
    return result


# ── Step caching ───────────────────────────────────────────────────────────────

_METADATA_FILE = "_working/metadata.json"


def _load_metadata(study_dir: Path) -> dict:
    p = study_dir / _METADATA_FILE
    return json.loads(p.read_text()) if p.exists() else {}


def _save_metadata(study_dir: Path, metadata: dict) -> None:
    p = study_dir / _METADATA_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(metadata, indent=2, default=str))


def _cache_key(tool_id: str, inputs: dict, params: dict) -> str:
    raw = (
        f"{tool_id}|"
        f"{json.dumps(inputs, sort_keys=True)}|"
        f"{json.dumps(params, sort_keys=True)}"
    )
    return hashlib.md5(raw.encode()).hexdigest()


def _is_cached(metadata: dict, key: str, inputs: dict) -> bool:
    entry = metadata.get(key)
    if not entry or entry.get("status") != "success":
        return False
    finished = entry.get("finished_time", 0)
    for path_str in inputs.values():
        p = Path(path_str)
        if not p.exists() or p.stat().st_mtime > finished:
            return False
    return True


def _mark_cached(metadata: dict, key: str) -> None:
    metadata[key] = {"status": "success", "finished_time": time.time()}


# ── Provenance ────────────────────────────────────────────────────────────────

def _write_provenance(
    resolved_outputs: dict[str, str],
    step_id: str,
    pipeline_id: str,
    container_image: str,
    params: dict,
    input_paths: dict,
    submitted_at: datetime | None,
    finished_at: datetime | None,
) -> None:
    """Write _provenance.json into each output directory after a successful step.

    Never raises — a failed provenance write must not block the pipeline.
    """
    payload = {
        "generated_at": (finished_at or datetime.now(timezone.utc)).isoformat(),
        "api_version": _API_VERSION,
        "pipeline_id": pipeline_id,
        "step_id": step_id,
        "container_image": container_image,
        "submitted_at": submitted_at.isoformat() if submitted_at else None,
        "params": params,
        "input_paths": input_paths,
    }
    # Collect the unique directories that this step wrote into.
    dirs: set[Path] = set()
    for path_str in resolved_outputs.values():
        p = Path(path_str)
        dirs.add(p if not p.suffix else p.parent)
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / "_provenance.json").write_text(json.dumps(payload, indent=2, default=str))
        except Exception as exc:
            _log.warning("Could not write provenance to %s: %s", d, exc)


# ── Run store persistence ─────────────────────────────────────────────────────

def _run_to_dict(run: _RunRecord) -> dict:
    return {
        "run_id": run.run_id,
        "user_id": run.user_id,
        "project_id": run.project_id,
        "pipeline_id": run.pipeline_id,
        "status": run.status,
        "submitted_at": run.submitted_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "current_step": run.current_step,
        "total_steps": run.total_steps,
        "error": run.error,
        "cancelled": run.cancelled,
        "backend_type": run.backend_type,
        "steps": [
            {
                "step_id": s.step_id,
                "tool_id": s.tool_id,
                "status": s.status,
                "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
                "finished_at": s.finished_at.isoformat() if s.finished_at else None,
                "job_id": s.job_id,
                "log_path": s.log_path,
                "container_image": s.container_image,
                "error": s.error,
                "logs": s.logs,
            }
            for s in run.steps
        ],
    }


def _run_from_dict(d: dict) -> _RunRecord:
    def _dt(s: str | None) -> datetime | None:
        return datetime.fromisoformat(s) if s else None

    run = _RunRecord(
        run_id=d["run_id"],
        user_id=d["user_id"],
        project_id=d["project_id"],
        pipeline_id=d["pipeline_id"],
        status=d["status"],
        submitted_at=_dt(d.get("submitted_at")) or datetime.now(timezone.utc),
        finished_at=_dt(d.get("finished_at")),
        current_step=d.get("current_step", 0),
        total_steps=d.get("total_steps", 0),
        error=d.get("error"),
        cancelled=d.get("cancelled", False),
        backend_type=d.get("backend_type", ""),
    )
    run.steps = [
        _StepRecord(
            step_id=s["step_id"],
            tool_id=s["tool_id"],
            status=s["status"],
            submitted_at=_dt(s.get("submitted_at")),
            finished_at=_dt(s.get("finished_at")),
            job_id=s.get("job_id"),
            log_path=s.get("log_path"),
            container_image=s.get("container_image"),
            error=s.get("error"),
            logs=s.get("logs", ""),
        )
        for s in d.get("steps", [])
    ]
    return run


def _persist_run(run: _RunRecord, study_dir: Path) -> None:
    """Write-through: update this run's entry in pipeline_runs.json."""
    runs_file = study_dir / "_working" / "pipeline_runs.json"
    runs_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing: dict = json.loads(runs_file.read_text()) if runs_file.exists() else {}
    except Exception:
        existing = {}
    existing[run.run_id] = _run_to_dict(run)
    runs_file.write_text(json.dumps(existing, indent=2, default=str))


def load_runs_from_disk(data_root: Path) -> None:
    """
    Called at startup: scan data_root for pipeline_runs.json files and
    populate the in-memory store.

    Handling of in-progress runs:
    - pending: always marked failed (task never started, cannot resume)
    - running + backend_type == "slurm": left as "running" so resume_runs()
      can reconnect to the still-running SLURM job
    - running + any other backend: marked failed (child process is gone)
    """
    if not data_root.exists():
        return
    for runs_file in data_root.rglob("_working/pipeline_runs.json"):
        try:
            records: dict = json.loads(runs_file.read_text())
        except Exception:
            continue
        for record in records.values():
            try:
                run = _run_from_dict(record)
            except Exception:
                continue
            if run.status == "pending":
                run.status = "failed"
                run.error = "Server restarted before run started"
                run.finished_at = run.finished_at or datetime.now(timezone.utc)
            elif run.status == "running" and run.backend_type != "slurm":
                run.status = "failed"
                run.error = "Server restarted while run was in progress"
                run.finished_at = run.finished_at or datetime.now(timezone.utc)
            # SLURM "running" runs are left intact; resume_runs() handles them.
            _runs[run.run_id] = run


def has_slurm_runs_to_resume() -> bool:
    """Return True if any loaded run is a SLURM run still marked 'running'."""
    return any(r.status == "running" and r.backend_type == "slurm" for r in _runs.values())


async def resume_runs(settings: Any, backend: JobBackend) -> None:
    """
    Resume SLURM pipeline runs that were in-progress at the last shutdown.

    Called at startup after load_runs_from_disk(). For each SLURM run still
    marked "running", this reconnects to the SLURM job and re-attaches a
    polling background task. Runs whose pipeline YAML is missing or whose
    backend does not support reconnection are marked failed.
    """
    for run in list(_runs.values()):
        if run.status != "running" or run.backend_type != "slurm":
            continue

        study_dir = settings.data_root / run.user_id / run.project_id

        try:
            from app.services import catalog_service
            pipeline = catalog_service.get_pipeline(settings.pipelines_path, run.pipeline_id)
        except Exception as exc:
            run.status = "failed"
            run.error = f"Cannot resume: pipeline '{run.pipeline_id}' not found: {exc}"
            run.finished_at = datetime.now(timezone.utc)
            _persist_run(run, study_dir)
            continue

        _log.info(
            "Resuming SLURM pipeline run %s (pipeline %s, step %d/%d)",
            run.run_id, run.pipeline_id, run.current_step, run.total_steps,
        )
        asyncio.create_task(
            run_pipeline_task(
                run=run,
                pipeline_steps=pipeline.steps,
                study_dir=study_dir,
                backend=backend,
                user_params={},      # params are already baked into the running SLURM job
                reuse_cached_steps=False,
                user_token=None,
                tools_path=settings.tools_path,
                s3_sync=None,
            )
        )


# ── Shared polling helper ─────────────────────────────────────────────────────

async def _poll_to_completion(
    handle: JobHandle,
    step: _StepRecord,
    run: _RunRecord,
    study_dir: Path | None = None,
) -> bool:
    """Poll handle until terminal. Updates step and run state. Returns True on success."""
    step.job_id = handle.job_id
    if handle.log_path:
        step.log_path = handle.log_path
    # Persist the job_id and log_path immediately so that a restart can reconnect
    # to a SLURM job that is still running on the cluster.
    if study_dir:
        _persist_run(run, study_dir)

    _active_handles[run.run_id] = handle
    try:
        while True:
            if run.cancelled:
                await handle.cancel()
                step.status = "failed"
                step.error = "Cancelled by user"
                run.status = "failed"
                run.error = "Cancelled by user"
                run.finished_at = datetime.now(timezone.utc)
                return False

            job_status = await handle.status()
            if job_status in ("succeeded", "failed"):
                break
            await asyncio.sleep(5)

        step.logs = await handle.logs()
        step.finished_at = datetime.now(timezone.utc)
    finally:
        _active_handles.pop(run.run_id, None)

    if job_status == "succeeded":
        step.status = "succeeded"
        return True

    step.status = "failed"
    step.error = "Job exited with non-zero status"
    return False


# ── S3 sync config ────────────────────────────────────────────────────────────

@dataclass
class S3SyncConfig:
    """S3 sync parameters for cloud-mode pipeline runs.

    When set, ``run_pipeline_task`` uploads the project directory to S3 before
    each step (so the Batch job can pull inputs) and downloads from S3 after
    each step (to retrieve outputs the Batch job wrote back).
    """

    bucket: str
    prefix: str   # Top-level prefix, e.g. "fsx". Full project prefix =
                  # {prefix}/{user_id}/{project_name}.


# ── Pipeline orchestration task ───────────────────────────────────────────────

async def run_pipeline_task(
    run: _RunRecord,
    pipeline_steps: list,          # list of PipelineStep (from catalog model)
    study_dir: Path,
    backend: JobBackend,
    user_params: dict[str, Any],
    reuse_cached_steps: bool,
    user_token: str | None,
    tools_path: Path,
    s3_sync: S3SyncConfig | None = None,
) -> None:
    """Background task: drive each pipeline step in order, with caching and error handling."""
    run.backend_type = backend.backend_name
    run.status = "running"
    _persist_run(run, study_dir)

    # Compute the project-scoped S3 prefix once, used for all steps.
    _s3_project_prefix: str | None = None
    if s3_sync:
        _s3_project_prefix = f"{s3_sync.prefix}/{run.user_id}/{study_dir.name}"

    metadata = _load_metadata(study_dir)
    step_outputs: dict[str, dict[str, str]] = {}

    for i, step_def in enumerate(pipeline_steps):
        if run.cancelled:
            run.status = "failed"
            run.error = "Cancelled by user"
            run.finished_at = datetime.now(timezone.utc)
            _persist_run(run, study_dir)
            return

        run.current_step = i
        step = run.steps[i]

        # Resolve ${STUDY} and ${prev.outputs.label} in path templates.
        # Done before the skip check so step_outputs can be populated correctly.
        resolved_inputs = {
            label: _resolve_path(val, study_dir, step_outputs)
            for label, val in (step_def.inputs or {}).items()
        }
        resolved_outputs = {
            label: _resolve_path(val, study_dir, step_outputs)
            for label, val in (step_def.outputs or {}).items()
        }
        mount_paths = {**resolved_inputs, **resolved_outputs}
        merged_params = {**user_params, **(step_def.params or {})}

        # Skip already-completed steps (SLURM restart recovery: resume mid-pipeline).
        if step.status in ("succeeded", "skipped"):
            step_outputs[step_def.id] = resolved_outputs
            continue
        if step.status == "failed":
            run.status = "failed"
            run.finished_at = run.finished_at or datetime.now(timezone.utc)
            _persist_run(run, study_dir)
            return

        key = _cache_key(step_def.tool, resolved_inputs, merged_params)

        # Check step cache (only for freshly-pending steps, not in-progress reconnects).
        if step.status == "pending" and reuse_cached_steps and _is_cached(metadata, key, resolved_inputs):
            step.status = "skipped"
            step.finished_at = datetime.now(timezone.utc)
            step_outputs[step_def.id] = resolved_outputs
            _persist_run(run, study_dir)
            continue

        # Determine the job handle — either reconnect to an in-progress SLURM job
        # (restart recovery) or submit a fresh job.
        handle: JobHandle | None = None
        tool_spec = None  # set below; needed for provenance

        if step.status == "running" and step.job_id:
            # This step was already submitted before the server restarted.
            # Attempt to reconnect via the backend (only SLURM supports this).
            handle = backend.reconnect(step.job_id, step.log_path)
            if handle is None:
                step.status = "failed"
                step.error = "Job lost at server restart (backend does not support reconnection)"
                run.status = "failed"
                run.error = f"Step '{step_def.id}' lost at server restart"
                run.finished_at = datetime.now(timezone.utc)
                _persist_run(run, study_dir)
                return
            _log.info(
                "Reconnected to %s job %s for step '%s' (run %s)",
                run.backend_type, step.job_id, step_def.id, run.run_id,
            )
            try:
                tool_spec = catalog_service.load_tool_spec(tools_path, step_def.tool)
            except FileNotFoundError:
                pass  # provenance will be skipped for this step

        if handle is None:
            # Fresh submission path.

            # Ensure output directories exist
            for path_str in resolved_outputs.values():
                Path(path_str).mkdir(parents=True, exist_ok=True)

            # Load tool spec (also used for provenance after success)
            try:
                tool_spec = catalog_service.load_tool_spec(tools_path, step_def.tool)
            except FileNotFoundError as e:
                step.status = "failed"
                step.error = str(e)
                run.status = "failed"
                run.error = f"Step '{step_def.id}': {e}"
                run.finished_at = datetime.now(timezone.utc)
                _persist_run(run, study_dir)
                return

            # Estimate subject count for cloud queue-drain metric and SLURM time limits.
            # Count NIfTI files in directory inputs; count file inputs as 1 each.
            def _count_subjects(p: Path) -> int:
                if p.is_dir():
                    return sum(1 for f in p.iterdir() if f.suffix in (".gz", ".nii"))
                return 1 if p.exists() else 0

            num_subjects = max(
                1,
                max((_count_subjects(Path(v)) for v in resolved_inputs.values()), default=0),
            ) if resolved_inputs else 1

            step.status = "running"
            step.submitted_at = datetime.now(timezone.utc)
            step.container_image = tool_spec.image

            # Upload project data to S3 before submitting so the Batch job can pull
            # the latest inputs. A sync failure aborts the step.
            if _s3_project_prefix:
                try:
                    await s3_sync_service.sync_to_s3(
                        study_dir, s3_sync.bucket, _s3_project_prefix  # type: ignore[union-attr]
                    )
                except Exception as exc:
                    step.status = "failed"
                    step.error = f"S3 pre-sync failed: {exc}"
                    run.status = "failed"
                    run.error = f"Step '{step_def.id}': S3 pre-sync failed: {exc}"
                    run.finished_at = datetime.now(timezone.utc)
                    _persist_run(run, study_dir)
                    return

            try:
                handle = await backend.submit(
                    tool_spec=tool_spec,
                    mount_paths={k: str(v) for k, v in mount_paths.items()},
                    params=merged_params,
                    num_subjects=num_subjects,
                    user_token=user_token,
                )
            except Exception as e:
                step.status = "failed"
                step.error = str(e)
                run.status = "failed"
                run.error = f"Step '{step_def.id}' submission failed: {e}"
                run.finished_at = datetime.now(timezone.utc)
                _persist_run(run, study_dir)
                return

        # Poll until terminal (passes study_dir so job_id/log_path are persisted immediately).
        success = await _poll_to_completion(handle, step, run, study_dir)

        # After the step finishes: sync S3 → local (pull Batch outputs), then
        # sync local → S3 (push any API-server-written state, e.g. metadata.json).
        # Both are best-effort — results are preserved in S3 regardless.
        if _s3_project_prefix:
            for direction, fn, args in [
                ("down", s3_sync_service.sync_from_s3,
                 (s3_sync.bucket, _s3_project_prefix, study_dir)),  # type: ignore[union-attr]
                ("up",   s3_sync_service.sync_to_s3,
                 (study_dir, s3_sync.bucket, _s3_project_prefix)),  # type: ignore[union-attr]
            ]:
                try:
                    await fn(*args)
                except Exception as exc:
                    _log.warning(
                        "S3 post-sync (%s) failed for step '%s' (data remains in S3): %s",
                        direction,
                        step_def.id,
                        exc,
                    )

        if not success:
            if not run.error:
                run.error = f"Step '{step_def.id}' failed"
            run.status = "failed"
            if not run.finished_at:
                run.finished_at = datetime.now(timezone.utc)
            _persist_run(run, study_dir)
            return

        step_outputs[step_def.id] = resolved_outputs
        # Provenance must be written BEFORE _mark_cached so that finished_time
        # (set by time.time() inside _mark_cached) is >= the provenance file's
        # mtime. If provenance were written after, its mtime would exceed
        # finished_time and any downstream step whose input dir overlaps this
        # step's output dir would appear dirty on the next cache check.
        if tool_spec:
            _write_provenance(
                resolved_outputs=resolved_outputs,
                step_id=step_def.id,
                pipeline_id=run.pipeline_id,
                container_image=tool_spec.image,
                params=merged_params,
                input_paths=resolved_inputs,
                submitted_at=step.submitted_at,
                finished_at=step.finished_at,
            )
        _mark_cached(metadata, key)
        _save_metadata(study_dir, metadata)
        _persist_run(run, study_dir)

    run.status = "succeeded"
    run.finished_at = datetime.now(timezone.utc)
    _persist_run(run, study_dir)


# ── Direct step execution (DICOM conversion, etc.) ────────────────────────────

@dataclass
class DirectStep:
    """A pre-resolved tool execution step — no pipeline YAML needed."""
    step_id: str
    tool_id: str
    mount_paths: dict[str, str]
    params: dict[str, Any] = field(default_factory=dict)


async def run_direct_steps_task(
    run: _RunRecord,
    direct_steps: list[DirectStep],
    backend: JobBackend,
    user_token: str | None,
    tools_path: Path,
    study_dir: Path | None = None,
) -> None:
    """Background task: execute a list of pre-resolved steps sequentially."""
    run.status = "running"
    if study_dir:
        _persist_run(run, study_dir)

    for i, step_def in enumerate(direct_steps):
        if run.cancelled:
            run.status = "failed"
            run.error = "Cancelled by user"
            run.finished_at = datetime.now(timezone.utc)
            if study_dir:
                _persist_run(run, study_dir)
            return

        run.current_step = i
        step = run.steps[i]
        step.status = "running"
        step.submitted_at = datetime.now(timezone.utc)

        try:
            tool_spec = catalog_service.load_tool_spec(tools_path, step_def.tool_id)
            handle = await backend.submit(
                tool_spec=tool_spec,
                mount_paths=step_def.mount_paths,
                params=step_def.params,
                user_token=user_token,
            )
        except Exception as e:
            step.status = "failed"
            step.error = str(e)
            run.status = "failed"
            run.error = str(e)
            run.finished_at = datetime.now(timezone.utc)
            if study_dir:
                _persist_run(run, study_dir)
            return

        success = await _poll_to_completion(handle, step, run)
        if not success:
            if not run.error:
                run.error = f"Step '{step_def.step_id}' failed"
            run.status = "failed"
            if not run.finished_at:
                run.finished_at = datetime.now(timezone.utc)
            if study_dir:
                _persist_run(run, study_dir)
            return

        if study_dir:
            _persist_run(run, study_dir)

    run.status = "succeeded"
    run.finished_at = datetime.now(timezone.utc)
    if study_dir:
        _persist_run(run, study_dir)


# ── Public API ─────────────────────────────────────────────────────────────────

def create_pipeline_run(
    user_id: str,
    project_id: str,
    pipeline_id: str,
    pipeline_steps: list,
) -> _RunRecord:
    """Create and register a pipeline run record (does not start the task)."""
    run_id = str(uuid.uuid4())
    run = _RunRecord(
        run_id=run_id,
        user_id=user_id,
        project_id=project_id,
        pipeline_id=pipeline_id,
        total_steps=len(pipeline_steps),
        steps=[_StepRecord(step_id=s.id, tool_id=s.tool) for s in pipeline_steps],
    )
    _runs[run_id] = run
    return run


def create_direct_run(
    user_id: str,
    project_id: str,
    pipeline_id: str,
    direct_steps: list[DirectStep],
) -> _RunRecord:
    """Create and register a direct-step run record (does not start the task)."""
    run_id = str(uuid.uuid4())
    run = _RunRecord(
        run_id=run_id,
        user_id=user_id,
        project_id=project_id,
        pipeline_id=pipeline_id,
        total_steps=len(direct_steps),
        steps=[_StepRecord(step_id=s.step_id, tool_id=s.tool_id) for s in direct_steps],
    )
    _runs[run_id] = run
    return run


def _to_summary(run: _RunRecord) -> PipelineRunSummary:
    return PipelineRunSummary(
        run_id=run.run_id,
        project_id=run.project_id,
        pipeline_id=run.pipeline_id,
        status=run.status,
        submitted_at=run.submitted_at,
        finished_at=run.finished_at,
        current_step=run.current_step,
        total_steps=run.total_steps,
    )


def _to_detail(run: _RunRecord) -> PipelineRunDetail:
    return PipelineRunDetail(
        run_id=run.run_id,
        project_id=run.project_id,
        pipeline_id=run.pipeline_id,
        status=run.status,
        submitted_at=run.submitted_at,
        finished_at=run.finished_at,
        current_step=run.current_step,
        total_steps=run.total_steps,
        error=run.error,
        steps=[
            StepStatus(
                step_id=s.step_id,
                tool_id=s.tool_id,
                status=s.status,
                submitted_at=s.submitted_at,
                finished_at=s.finished_at,
                job_id=s.job_id,
                container_image=s.container_image,
                error=s.error,
            )
            for s in run.steps
        ],
    )


def list_runs(
    user_id: str,
    project_id: str | None,
    limit: int,
) -> list[PipelineRunSummary]:
    runs = [
        r for r in _runs.values()
        if r.user_id == user_id
        and (project_id is None or r.project_id == project_id)
    ]
    runs.sort(key=lambda r: r.submitted_at, reverse=True)
    return [_to_summary(r) for r in runs[:limit]]


def get_run_detail(run_id: str, user_id: str) -> PipelineRunDetail:
    run = _runs.get(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' not found")
    if run.user_id != user_id:
        raise HTTPException(403, "Access denied")
    return _to_detail(run)


async def get_run_logs(run_id: str, user_id: str) -> PipelineRunLogs:
    run = _runs.get(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' not found")
    if run.user_id != user_id:
        raise HTTPException(403, "Access denied")

    parts: list[str] = []
    active_handle = _active_handles.get(run_id)
    for s in run.steps:
        if s.status == "running" and active_handle is not None:
            try:
                live_logs = await active_handle.logs()
            except Exception:
                live_logs = s.logs
        else:
            live_logs = s.logs
        if live_logs:
            parts.append(f"=== Step {s.step_id} ===\n{live_logs}")

    return PipelineRunLogs(run_id=run_id, logs="\n".join(parts))


def cancel_run(run_id: str, user_id: str) -> None:
    run = _runs.get(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' not found")
    if run.user_id != user_id:
        raise HTTPException(403, "Access denied")
    run.cancelled = True
