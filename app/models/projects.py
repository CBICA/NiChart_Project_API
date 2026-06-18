"""
Request/response schemas for project management endpoints.
"""

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# Allowed project name pattern: start with alphanumeric, then alphanumeric/hyphen/underscore, max 64 chars
_PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class ProjectCreate(BaseModel):
    """Request body for creating a new project."""

    name: str = Field(
        description=(
            "Project name. Used directly as the directory name and URL path segment. "
            "Must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$."
        )
    )

    @field_validator("name")
    @classmethod
    def name_must_be_safe(cls, v: str) -> str:
        if not _PROJECT_NAME_RE.match(v):
            raise ValueError(
                "Project name must start with a letter or digit and contain only "
                "letters, digits, hyphens, and underscores (max 64 characters)."
            )
        return v


class Project(BaseModel):
    """A user project."""

    id: str = Field(description="Project identifier (same as directory name).")
    created_at: datetime | None = Field(
        default=None, description="Creation timestamp (derived from directory mtime)."
    )
