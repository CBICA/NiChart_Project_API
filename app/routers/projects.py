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
    description=(
        "Returns all projects owned by the authenticated user."
        "\n\n"
        "Projects whose retention expired more than PROJECT_EXPIRY_GRACE_HOURS ago are "
        "deleted as part of serving this request and are not listed. Expired projects "
        "still inside the grace window are listed as normal; the client is expected to "
        "warn about them (see ``GET /projects/{project_id}/retention``)."
    ),
    response_model=list[Project],
    responses={401: _COMMON_ERRORS[401]},
)
async def list_projects(
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> list[Project]:
    projects = file_service.list_projects(settings, user)
    reaped = await file_service.reap_expired_projects(
        settings, user, [p.id for p in projects]
    )
    return [p for p in projects if p.id not in reaped]


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
    description=(
        "Permanently deletes the project and all its data. Irreversible."
        "\n\n"
        "When a data bucket is configured the project's objects are deleted from cloud "
        "storage as part of this request. If that fails nothing at all is deleted and "
        "the response is 502, so the request can be retried."
    ),
    status_code=204,
    responses={
        **{k: _COMMON_ERRORS[k] for k in (401, 404)},
        502: {
            "model": ErrorDetail,
            "description": "Cloud storage could not be cleared; nothing was deleted.",
        },
    },
)
async def delete_project(
    project_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    await file_service.delete_project(settings, user, project_id)


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
        "An expiry already more than PROJECT_EXPIRY_GRACE_HOURS in the past means the "
        "project is deleted as part of serving this request, and the response is 404. A "
        "project whose expiry is in the past but within the grace window is returned "
        "normally, with ``expires_at`` in the past, for the client to warn about."
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
    expiry = await retention_service.resolve_expiry(
        bucket=settings.s3_data_bucket,
        prefix=settings.s3_data_prefix,
        user_id=user.sub,
        project_id=project_id,
        project_dir=pdir,
        retention_days=settings.project_retention_days,
    )
    if await file_service.reap_if_past_grace(settings, user, project_id, pdir, expiry):
        raise HTTPException(404, f"Project '{project_id}' not found")
    if expiry is None:
        if not settings.s3_data_bucket:
            raise HTTPException(404, "Retention tracking is not available in local mode.")
        raise HTTPException(404, "Retention heartbeat not found — the project may not have one yet.")
    return RetentionInfo(expires_at=expiry)


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
        "A project already more than PROJECT_EXPIRY_GRACE_HOURS past its expiry cannot be "
        "revived: it is deleted as part of serving this request and the response is 404, "
        "the same as any other query. Refreshing it instead would make a project's "
        "survival depend on which endpoint the client happened to call first."
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
    if await file_service.reap_project_if_expired(settings, user, project_id):
        raise HTTPException(404, f"Project '{project_id}' not found")
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
