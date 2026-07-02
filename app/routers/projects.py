"""
Project management — authenticated.

A project maps 1-to-1 with a directory under
``NICHART_DATA_ROOT/{user_sub}/{project_id}/``.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import CurrentUser, require_auth
from app.config import Settings, get_settings
from app.models.errors import ErrorDetail
from app.models.projects import Project, ProjectCreate, RetentionInfo
from app.services import file_service, retention_service

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["Projects"])

_COMMON_ERRORS = {
    400: {"model": ErrorDetail, "description": "Bad request (e.g. invalid project name)."},
    401: {"model": ErrorDetail, "description": "Missing or invalid token."},
    404: {"model": ErrorDetail, "description": "Project not found."},
    409: {"model": ErrorDetail, "description": "A project with that name already exists."},
}


@router.get(
    "",
    summary="List projects",
    description="Returns all projects owned by the authenticated user.",
    response_model=list[Project],
    responses={401: _COMMON_ERRORS[401]},
)
async def list_projects(
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> list[Project]:
    return file_service.list_projects(settings, user)


@router.post(
    "",
    summary="Create a project",
    description=(
        "Creates a new project directory. "
        "The project name is used as the directory name and URL path segment. "
        "Must match ``^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$``. "
        "\n\n"
        "In cloud mode a retention heartbeat is written to S3 as part of creation, "
        "which starts the PROJECT_RETENTION_DAYS countdown."
    ),
    response_model=Project,
    status_code=201,
    responses={k: _COMMON_ERRORS[k] for k in (400, 401, 409)},
)
async def create_project(
    body: ProjectCreate,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> Project:
    project = file_service.create_project(settings, user, body.name)
    if settings.s3_data_bucket:
        try:
            await retention_service.write_heartbeat(
                bucket=settings.s3_data_bucket,
                prefix=settings.s3_data_prefix,
                user_id=user.sub,
                project_id=body.name,
            )
        except Exception:
            # Heartbeat failure does not fail project creation — the Lambda
            # sweep will backfill missing heartbeats on its next daily run.
            _log.exception(
                "Failed to write retention heartbeat for new project %r (user %r); "
                "Lambda will backfill on next sweep.",
                body.name,
                user.sub,
            )
    return project


@router.delete(
    "/{project_id}",
    summary="Delete a project",
    description="Permanently deletes the project and all its data. Irreversible.",
    status_code=204,
    responses={k: _COMMON_ERRORS[k] for k in (401, 404)},
)
async def delete_project(
    project_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    file_service.delete_project(settings, user, project_id)


# ── Retention endpoints ───────────────────────────────────────────────────────

_RETENTION_ERRORS = {
    **{k: _COMMON_ERRORS[k] for k in (401, 404)},
    404: {"model": ErrorDetail, "description": "Project not found or heartbeat not yet written (legacy project or creation race)."},
}


@router.get(
    "/{project_id}/retention",
    summary="Get project retention info",
    description=(
        "Returns the expiry timestamp for the project. "
        "The countdown resets each time ``POST /projects/{project_id}/retention/refresh`` "
        "is called. "
        "\n\n"
        "Returns 404 when the heartbeat object is absent — this is expected for projects "
        "created before this feature shipped; the Lambda sweep will backfill the heartbeat "
        "on its next daily run, after which this endpoint will return a value."
        "\n\n"
        "Cloud mode only — returns 404 in local mode."
    ),
    response_model=RetentionInfo,
    responses=_RETENTION_ERRORS,
)
async def get_retention(
    project_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> RetentionInfo:
    pdir = file_service.resolve_project(settings, user, project_id)
    override = retention_service.read_retention_override(pdir)
    if override is not None:
        return RetentionInfo(expires_at=override)
    if not settings.s3_data_bucket:
        raise HTTPException(404, "Retention tracking is not available in local mode.")
    last_modified = await retention_service.read_heartbeat(
        bucket=settings.s3_data_bucket,
        prefix=settings.s3_data_prefix,
        user_id=user.sub,
        project_id=project_id,
    )
    if last_modified is None:
        raise HTTPException(404, "Retention heartbeat not found — the project may not have one yet.")
    return RetentionInfo(
        expires_at=retention_service.expires_at(last_modified, settings.project_retention_days)
    )


@router.post(
    "/{project_id}/retention/refresh",
    summary="Refresh project retention",
    description=(
        "Resets the retention countdown by overwriting the heartbeat marker with the "
        "current timestamp. Returns the new expiry time (now + PROJECT_RETENTION_DAYS). "
        "\n\n"
        "If the heartbeat was previously absent (legacy project) this call creates it, "
        "which is equivalent to a fresh creation. "
        "\n\n"
        "Cloud mode only — returns 404 in local mode."
    ),
    response_model=RetentionInfo,
    responses=_RETENTION_ERRORS,
)
async def refresh_retention(
    project_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> RetentionInfo:
    pdir = file_service.resolve_project(settings, user, project_id)
    retention_service.clear_retention_override(pdir)  # test override no longer applies after a real refresh
    if not settings.s3_data_bucket:
        raise HTTPException(404, "Retention tracking is not available in local mode.")
    written_at = await retention_service.write_heartbeat(
        bucket=settings.s3_data_bucket,
        prefix=settings.s3_data_prefix,
        user_id=user.sub,
        project_id=project_id,
    )
    return RetentionInfo(
        expires_at=retention_service.expires_at(written_at, settings.project_retention_days)
    )
