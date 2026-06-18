"""
Request/response schemas for file management endpoints.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FileEntry(BaseModel):
    """A single node in the project file tree."""

    name: str = Field(description="Filename or directory name.")
    path: str = Field(description="Path relative to the project root.")
    type: Literal["file", "directory"] = Field(description="Node kind.")
    size: int | None = Field(default=None, description="File size in bytes (files only).")
    mtime: float | None = Field(default=None, description="Last-modified UNIX timestamp.")


class DirectoryTree(BaseModel):
    """Flat listing of all visible nodes in a project directory."""

    entries: list[FileEntry] = Field(default_factory=list)


# ── NIfTI upload / staging ───────────────────────────────────────────────────

class NiftiUploadProposal(BaseModel):
    """Server-inferred metadata for a single uploaded NIfTI file."""

    filename: str = Field(description="Original uploaded filename.")
    inferred_mrid: str | None = Field(
        default=None, description="MRID inferred from the filename by stripping known suffixes."
    )
    inferred_modality: str | None = Field(
        default=None,
        description="Modality inferred from the filename (t1/fl/t2/t1ce/adc), or null if not detectable.",
    )


class NiftiStagingResult(BaseModel):
    """Response after uploading NIfTI file(s); requires a follow-up commit call."""

    staging_id: str = Field(description="Opaque staging area identifier. Pass to the commit endpoint.")
    proposals: list[NiftiUploadProposal] = Field(
        description="Server's best-effort mapping of filenames to MRID and modality."
    )


class NiftiMapping(BaseModel):
    """Confirmed mapping for a single staged NIfTI file."""

    filename: str = Field(description="Filename as returned in the staging proposals.")
    mrid: str = Field(description="Subject identifier. Becomes the file stem in the target directory.")
    modality: Literal["t1", "fl", "t2", "t1ce", "adc"] = Field(
        description="NiChart modality label. Determines which subdirectory the file lands in."
    )


class NiftiCommitRequest(BaseModel):
    """Request body for the NIfTI staging commit endpoint."""

    mappings: list[NiftiMapping] = Field(
        description="One entry per staged file; every staged file must be accounted for."
    )


class CommittedFile(BaseModel):
    mrid: str
    modality: str
    path: str = Field(description="Path relative to the project root where the file was written.")


class NiftiCommitResult(BaseModel):
    """Response after a successful NIfTI commit."""

    committed: list[CommittedFile]


# ── Participants CSV ──────────────────────────────────────────────────────────

class ParticipantRow(BaseModel):
    """One row from participants.csv.

    ``MRID`` is required. Any additional columns (Age, Sex, MMSE, etc.) are
    preserved as extra fields and round-trip through the API unchanged.
    """

    model_config = ConfigDict(extra="allow")

    MRID: str = Field(description="Subject identifier. Must match the stem of uploaded NIfTI files.")


class ParticipantsList(BaseModel):
    """List of participant rows."""

    rows: list[ParticipantRow] = Field(default_factory=list)


class ParticipantsUpdate(BaseModel):
    """Request body to replace the participants list."""

    rows: list[dict[str, Any]] = Field(
        description=(
            "Complete replacement list. Each row must contain an 'MRID' key. "
            "Any additional keys are written as extra columns in participants.csv. "
            "Existing participants.csv is overwritten."
        )
    )
