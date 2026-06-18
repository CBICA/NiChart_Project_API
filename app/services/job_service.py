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
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.backends.base import JobBackend, JobHandle
from app.models.jobs import (
    PipelineRunCreated,
    PipelineRunDetail,
    PipelineRunLogs,
    PipelineRunSummary,
    StepStatus,
)
from app.services import catalog_service


# ── Run store ──────────────────────────────────────────────────────────────────

@dataclass
class _StepRecord:
    step_id: str
    tool_id: str
    status: str = "pending"
    submitted_at: datetime | None = None
    finished_at: datetime | None = None
    job_id: str | None = None
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


_runs: dict[str, _RunRecord] = {}


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
        "steps": [
            {
                "step_id": s.step_id,
                "tool_id": s.tool_id,
                "status": s.status,
                "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
                "finished_at": s.finished_at.isoformat() if s.finished_at else None,
                "job_id": s.job_id,
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
    )
    run.steps = [
        _StepRecord(
            step_id=s["step_id"],
            tool_id=s["tool_id"],
            status=s["status"],
            submitted_at=_dt(s.get("submitted_at")),
            finished_at=_dt(s.get("finished_at")),
            job_id=s.get("job_id"),
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
    populate the in-memory store. Runs that were in-progress at shutdown are
    marked failed — their background tasks no longer exist.
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
            if run.status in ("pending", "running"):
                run.status = "failed"
                run.error = "Server restarted while run was in progress"
                run.finished_at = run.finished_at or datetime.now(timezone.utc)
            _runs[run.run_id] = run


# ── Shared polling helper ─────────────────────────────────────────────────────

async def _poll_to_completion(
    handle: JobHandle,
    step: _StepRecord,
    run: _RunRecord,
) -> bool:
    """Poll handle until terminal. Updates step and run state. Returns True on success."""
    step.job_id = handle.job_id

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

    if job_status == "succeeded":
        step.status = "succeeded"
        return True

    step.status = "failed"
    step.error = "Job exited with non-zero status"
    return False


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
) -> None:
    """Background task: drive each pipeline step in order, with caching and error handling."""
    run.status = "running"
    _persist_run(run, study_dir)

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

        # Resolve ${STUDY} and ${prev.outputs.label} in path templates
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

        # Check step cache
        key = _cache_key(step_def.tool, resolved_inputs, merged_params)
        if reuse_cached_steps and _is_cached(metadata, key, resolved_inputs):
            step.status = "skipped"
            step.finished_at = datetime.now(timezone.utc)
            step_outputs[step_def.id] = resolved_outputs
            _persist_run(run, study_dir)
            continue

        # Ensure output directories exist
        for path_str in resolved_outputs.values():
            Path(path_str).mkdir(parents=True, exist_ok=True)

        # Load tool spec
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

        # Estimate subject count for cloud queue drain metric
        num_subjects = max(
            1,
            sum(1 for v in resolved_inputs.values() if Path(v).is_file()),
        ) if resolved_inputs else 1

        step.status = "running"
        step.submitted_at = datetime.now(timezone.utc)

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

        success = await _poll_to_completion(handle, step, run)
        if not success:
            if not run.error:
                run.error = f"Step '{step_def.id}' failed"
            run.status = "failed"
            if not run.finished_at:
                run.finished_at = datetime.now(timezone.utc)
            _persist_run(run, study_dir)
            return

        step_outputs[step_def.id] = resolved_outputs
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


def get_run_logs(run_id: str, user_id: str) -> PipelineRunLogs:
    run = _runs.get(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' not found")
    if run.user_id != user_id:
        raise HTTPException(403, "Access denied")
    all_logs = "\n".join(
        f"=== Step {s.step_id} ===\n{s.logs}"
        for s in run.steps
        if s.logs
    )
    return PipelineRunLogs(run_id=run_id, logs=all_logs)


def cancel_run(run_id: str, user_id: str) -> None:
    run = _runs.get(run_id)
    if not run:
        raise HTTPException(404, f"Run '{run_id}' not found")
    if run.user_id != user_id:
        raise HTTPException(403, "Access denied")
    run.cancelled = True
