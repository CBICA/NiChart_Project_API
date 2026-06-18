"""
Project management — authenticated.

A project maps 1-to-1 with a directory under
``NICHART_DATA_ROOT/{user_sub}/{project_id}/``.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import CurrentUser, require_auth
from app.config import Settings, get_settings
from app.models.errors import ErrorDetail
from app.models.projects import Project, ProjectCreate
from app.services import file_service

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
        "Must match ``^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$``."
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
    return file_service.create_project(settings, user, body.name)


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
