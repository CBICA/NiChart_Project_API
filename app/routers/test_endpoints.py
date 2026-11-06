"""
Test/debug convenience endpoints — only mounted when NICHART_ENABLE_TEST_ENDPOINTS=true.

These bypass normal constraints and must never be enabled in production.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.dependencies import CurrentUser, require_auth
from app.config import Settings, get_settings
from app.models.projects import Project
from app.services import file_service, retention_service

router = APIRouter(prefix="/test", tags=["Test"])


class ExpireProjectRequest(BaseModel):
    """Request body for creating a project that appears close to expiry."""

    name: str = Field(description="Project name. Same rules as POST /projects.")
    expires_in_minutes: int = Field(
        default=30,
        ge=-525600,
        le=1440,
        description=(
            "How many minutes from now the project should appear to expire. Negative "
            "values put the expiry in the past, which is how the automatic deletion of "
            "long-expired projects is exercised: a value more negative than "
            "PROJECT_EXPIRY_GRACE_HOURS means the next query for this project deletes "
            "it and answers 404."
        ),
    )


@router.post(
    "/projects/expire-soon",
    summary="[TEST] Create a project that appears about to expire",
    description=(
        "Creates a project and sets a local retention override so that "
        "``GET /projects/{project_id}/retention`` returns an expiry time of "
        "``now + expires_in_minutes``. "
        "Useful for testing the frontend warning UI without waiting 7 days, and — with "
        "a negative ``expires_in_minutes`` — the automatic deletion that follows the "
        "grace window. "
        "\n\n"
        "The override is stored in ``_working/retention_expires_at_override`` inside "
        "the project directory. It is cleared if the user calls the real "
        "``POST /projects/{project_id}/retention/refresh`` endpoint."
    ),
    response_model=Project,
    status_code=201,
)
async def create_expiring_project(
    body: ExpireProjectRequest,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> Project:
    project = file_service.create_project(settings, user, body.name)
    pdir = file_service.resolve_project(settings, user, body.name)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=body.expires_in_minutes)
    retention_service.write_retention_override(pdir, expires_at)
    return project
