"""
Pipeline job submission and status — authenticated.

Pipelines run asynchronously: submission returns a ``run_id`` immediately and
the actual orchestration (submit each tool step, poll, advance) happens in a
server-side background task. Clients poll ``GET /jobs/pipelines/{run_id}``.

URL structure:
  POST   /projects/{project_id}/jobs/pipelines  — submit (scoped to a project)
  GET    /jobs/pipelines                         — list (optional project filter)
  GET    /jobs/pipelines/{run_id}                — detail + per-step status
  GET    /jobs/pipelines/{run_id}/logs           — aggregated step logs
  DELETE /jobs/pipelines/{run_id}                — cancel
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from app.auth.dependencies import CurrentUser, require_auth
from app.backends import get_backend
from app.backends.base import JobBackend
from app.config import Settings, get_settings
from app.models.errors import ErrorDetail
from app.models.jobs import (
    PipelineRunDetail,
    PipelineRunLogs,
    PipelineRunSubmit,
    PipelineRunSummary,
)
from app.services import catalog_service, file_service, job_service

# Two routers: one project-scoped (for submission), one top-level (for queries)
project_router = APIRouter(prefix="/projects/{project_id}/jobs", tags=["Jobs"])
jobs_router = APIRouter(prefix="/jobs", tags=["Jobs"])

_AUTH_ERRORS = {
    401: {"model": ErrorDetail, "description": "Missing or invalid token."},
    403: {"model": ErrorDetail, "description": "Access denied."},
}


# ── Submission (project-scoped) ───────────────────────────────────────────────

@project_router.post(
    "/pipelines",
    summary="Submit a pipeline run",
    description=(
        "Starts an asynchronous pipeline run for the given project. "
        "The server spawns a background task that executes each pipeline step in "
        "order, polling the job backend between steps. "
        "Use the returned ``run_id`` to track progress via ``GET /jobs/pipelines/{run_id}``."
    ),
    response_model=PipelineRunDetail,
    status_code=202,
    responses={
        **_AUTH_ERRORS,
        400: {"model": ErrorDetail, "description": "Invalid pipeline ID or parameters."},
        404: {"model": ErrorDetail, "description": "Project or pipeline not found."},
    },
)
async def submit_pipeline(
    project_id: str,
    body: PipelineRunSubmit,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    backend: JobBackend = Depends(get_backend),
) -> PipelineRunDetail:
    pdir = file_service.resolve_project(settings, user, project_id)
    pipeline = catalog_service.get_pipeline(settings.pipelines_path, body.pipeline_id)
    run = job_service.create_pipeline_run(
        user_id=user.sub,
        project_id=project_id,
        pipeline_id=body.pipeline_id,
        pipeline_steps=pipeline.steps,
    )
    background_tasks.add_task(
        job_service.run_pipeline_task,
        run=run,
        pipeline_steps=pipeline.steps,
        study_dir=pdir,
        backend=backend,
        user_params=body.params,
        reuse_cached_steps=body.reuse_cached_steps,
        user_token=user.token,
        tools_path=settings.tools_path,
    )
    return job_service.get_run_detail(run.run_id, user.sub)


# ── Queries (top-level, optional project filter) ──────────────────────────────

@jobs_router.get(
    "/pipelines",
    summary="List pipeline runs",
    description=(
        "Returns the authenticated user's pipeline runs in reverse-chronological order. "
        "Pass ``project_id`` to filter to a single project."
    ),
    response_model=list[PipelineRunSummary],
    responses=_AUTH_ERRORS,
)
async def list_pipeline_runs(
    project_id: str | None = Query(default=None, description="Filter runs to this project."),
    limit: int = Query(default=50, ge=1, le=200, description="Maximum number of runs to return."),
    user: CurrentUser = Depends(require_auth),
) -> list[PipelineRunSummary]:
    return job_service.list_runs(user_id=user.sub, project_id=project_id, limit=limit)


@jobs_router.get(
    "/pipelines/{run_id}",
    summary="Get pipeline run detail",
    description=(
        "Returns the full run record including per-step status, timestamps, "
        "and any error messages."
    ),
    response_model=PipelineRunDetail,
    responses={**_AUTH_ERRORS, 404: {"model": ErrorDetail}},
)
async def get_pipeline_run(
    run_id: str,
    user: CurrentUser = Depends(require_auth),
) -> PipelineRunDetail:
    return job_service.get_run_detail(run_id=run_id, user_id=user.sub)


@jobs_router.get(
    "/pipelines/{run_id}/logs",
    summary="Get pipeline run logs",
    description=(
        "Returns concatenated log output from all steps completed so far. "
        "Poll this endpoint alongside the status endpoint during a run."
    ),
    response_model=PipelineRunLogs,
    responses={**_AUTH_ERRORS, 404: {"model": ErrorDetail}},
)
async def get_pipeline_logs(
    run_id: str,
    user: CurrentUser = Depends(require_auth),
) -> PipelineRunLogs:
    return job_service.get_run_logs(run_id=run_id, user_id=user.sub)


@jobs_router.delete(
    "/pipelines/{run_id}",
    summary="Cancel a pipeline run",
    description=(
        "Requests cancellation of a running pipeline. "
        "The currently executing tool step is cancelled on the backend; "
        "subsequent steps will not be submitted. "
        "No-op if the run is already in a terminal state."
    ),
    status_code=204,
    responses={**_AUTH_ERRORS, 404: {"model": ErrorDetail}},
)
async def cancel_pipeline_run(
    run_id: str,
    user: CurrentUser = Depends(require_auth),
) -> None:
    job_service.cancel_run(run_id=run_id, user_id=user.sub)
