"""
File management within a project — authenticated.

All paths from the client are validated against the project root before any I/O.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.auth.dependencies import CurrentUser, require_auth
from app.config import Settings, get_settings
from app.models.errors import ErrorDetail
from app.models.files import (
    DirectoryTree,
    NiftiCommitRequest,
    NiftiCommitResult,
    NiftiStagingResult,
    ParticipantsList,
    ParticipantsUpdate,
)
from app.models.readiness import ReadinessReport
from app.services import catalog_service, file_service, readiness_service

router = APIRouter(prefix="/projects/{project_id}", tags=["Files"])

_AUTH_ERRORS = {
    401: {"model": ErrorDetail, "description": "Missing or invalid token."},
    403: {"model": ErrorDetail, "description": "Access denied to this project."},
    404: {"model": ErrorDetail, "description": "Project or file not found."},
    400: {"model": ErrorDetail, "description": "Invalid or unsafe path."},
}


# ── Directory tree ────────────────────────────────────────────────────────────

@router.get(
    "/files",
    summary="List project files",
    description=(
        "Returns a flat listing of all visible files and directories in the project. "
        "Internal staging (``_upload/``) and working (``_working/``) directories are excluded."
    ),
    response_model=DirectoryTree,
    responses=_AUTH_ERRORS,
)
async def list_files(
    project_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> DirectoryTree:
    pdir = file_service.resolve_project(settings, user, project_id)
    return file_service.list_project_files(pdir)


# ── Download ──────────────────────────────────────────────────────────────────

@router.get(
    "/files/download",
    summary="Download a file or directory",
    description=(
        "Download a single file, or a directory as a streaming zip archive when "
        "``zip=true``. The ``path`` is relative to the project root and is validated "
        "to prevent traversal."
    ),
    response_model=None,
    responses={
        200: {"description": "File content or zip stream."},
        **_AUTH_ERRORS,
    },
)
async def download_file(
    project_id: str,
    path: str = Query(description="Path relative to the project root."),
    zip: bool = Query(default=False, description="Stream target directory as a zip archive."),
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse | FileResponse:
    pdir = file_service.resolve_project(settings, user, project_id)
    target = file_service.resolve_file_path(pdir, path)

    if zip or target.is_dir():
        if not target.is_dir():
            raise HTTPException(400, "zip=true requires a directory path")
        data = file_service.zip_directory_bytes(target)
        return StreamingResponse(
            iter([data]),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{target.name}.zip"'},
        )

    return FileResponse(path=target, filename=target.name)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete(
    "/files",
    summary="Delete a file or directory",
    description="Permanently deletes the file or directory at ``path``.",
    status_code=204,
    responses=_AUTH_ERRORS,
)
async def delete_file(
    project_id: str,
    path: str = Query(description="Path relative to the project root."),
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    import shutil
    pdir = file_service.resolve_project(settings, user, project_id)
    target = file_service.resolve_file_path(pdir, path)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


# ── NIfTI upload — stage ──────────────────────────────────────────────────────

@router.post(
    "/files/upload/nifti",
    summary="Upload NIfTI file(s) to staging",
    description=(
        "Accepts one or more ``.nii`` / ``.nii.gz`` files. Files land in the project "
        "staging area and the server returns its best-effort MRID and modality inference. "
        "Follow up with the commit endpoint to move them into the project."
    ),
    response_model=NiftiStagingResult,
    status_code=202,
    responses=_AUTH_ERRORS,
)
async def upload_nifti(
    project_id: str,
    files: list[UploadFile],
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> NiftiStagingResult:
    pdir = file_service.resolve_project(settings, user, project_id)
    filenames = [f.filename or "" for f in files]
    file_data = [await f.read() for f in files]
    return file_service.stage_nifti_files(pdir, filenames, file_data)


# ── NIfTI upload — commit ─────────────────────────────────────────────────────

@router.post(
    "/files/stage/{staging_id}/commit",
    summary="Commit staged NIfTI files",
    description=(
        "Confirms MRID and modality mappings for staged files and moves them into "
        "their final locations. Every staged file must appear in ``mappings``."
    ),
    response_model=NiftiCommitResult,
    responses=_AUTH_ERRORS,
)
async def commit_nifti(
    project_id: str,
    staging_id: str,
    body: NiftiCommitRequest,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> NiftiCommitResult:
    pdir = file_service.resolve_project(settings, user, project_id)
    return file_service.commit_nifti_staging(pdir, staging_id, body.mappings)


# ── Staging discard ───────────────────────────────────────────────────────────

@router.delete(
    "/files/stage/{staging_id}",
    summary="Discard a staging area",
    description="Deletes all files in the staging area without committing.",
    status_code=204,
    responses=_AUTH_ERRORS,
)
async def discard_staging(
    project_id: str,
    staging_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    pdir = file_service.resolve_project(settings, user, project_id)
    file_service.discard_nifti_staging(pdir, staging_id)


# ── CSV upload ────────────────────────────────────────────────────────────────

@router.post(
    "/files/upload/csv",
    summary="Upload participants CSV",
    description=(
        "Replaces ``participants/participants.csv`` with the uploaded file. "
        "The file is stored as-is; must have at least an ``MRID`` column."
    ),
    status_code=204,
    responses=_AUTH_ERRORS,
)
async def upload_csv(
    project_id: str,
    file: UploadFile,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    pdir = file_service.resolve_project(settings, user, project_id)
    contents = await file.read()
    file_service.store_csv_upload(pdir, contents, file.filename or "")


# ── BIDS upload ───────────────────────────────────────────────────────────────

@router.post(
    "/files/upload/bids",
    summary="Upload a BIDS dataset",
    description=(
        "Accepts a ``.zip`` archive containing a BIDS-layout dataset, extracts it, "
        "and reorganises it into the NiChart project layout. "
        "``sub-{id}_T1w.nii.gz`` → ``t1/{id}.nii.gz``, ``_FLAIR`` → ``fl/``, etc. "
        "A ``participants.tsv`` at the archive root is converted to ``participants/participants.csv``."
    ),
    status_code=202,
    responses=_AUTH_ERRORS,
)
async def upload_bids(
    project_id: str,
    file: UploadFile,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    pdir = file_service.resolve_project(settings, user, project_id)
    contents = await file.read()
    file_service.store_bids_upload(pdir, contents, file.filename or "")


# ── IDAT upload ───────────────────────────────────────────────────────────────

@router.post(
    "/files/upload/idat",
    summary="Upload IDAT files",
    description="Accepts a ``.zip`` of ``.idat`` files. Extracted to ``idat/`` in the project.",
    status_code=204,
    responses=_AUTH_ERRORS,
)
async def upload_idat(
    project_id: str,
    file: UploadFile,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    pdir = file_service.resolve_project(settings, user, project_id)
    contents = await file.read()
    file_service.store_idat_upload(pdir, contents, file.filename or "")


# ── Participants ──────────────────────────────────────────────────────────────

@router.get(
    "/participants",
    summary="Get participants list",
    description="Returns the contents of ``participants/participants.csv`` as JSON rows.",
    response_model=ParticipantsList,
    responses={**_AUTH_ERRORS, 404: {"model": ErrorDetail}},
)
async def get_participants(
    project_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> ParticipantsList:
    pdir = file_service.resolve_project(settings, user, project_id)
    return file_service.read_participants(pdir)


@router.patch(
    "/participants",
    summary="Replace participants list",
    description=(
        "Overwrites ``participants/participants.csv`` with the provided rows. "
        "Full replacement — rows not included are removed."
    ),
    status_code=204,
    responses=_AUTH_ERRORS,
)
async def update_participants(
    project_id: str,
    body: ParticipantsUpdate,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    pdir = file_service.resolve_project(settings, user, project_id)
    file_service.write_participants(pdir, body.rows)


# ── Readiness check ───────────────────────────────────────────────────────────

@router.get(
    "/readiness/{pipeline_id}",
    summary="Check pipeline readiness",
    description=(
        "Evaluates whether the project has the data required to run a pipeline. "
        "Returns a per-requirement status so the client can guide users to upload "
        "missing imaging data or fill missing participants.csv columns. "
        "Imaging checks scan the modality directories (``t1/``, ``fl/``, etc.) for NIfTI files. "
        "CSV checks verify that required columns exist and are non-empty for every subject."
    ),
    response_model=ReadinessReport,
    responses=_AUTH_ERRORS,
)
async def check_readiness(
    project_id: str,
    pipeline_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> ReadinessReport:
    pdir = file_service.resolve_project(settings, user, project_id)
    raw_requires = catalog_service.get_pipeline_raw_requires(settings.pipelines_path, pipeline_id)
    return readiness_service.check_readiness(pdir, pipeline_id, raw_requires)
