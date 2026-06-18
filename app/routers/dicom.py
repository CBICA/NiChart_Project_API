"""
DICOM staging, inspection, and conversion — authenticated.

DICOM → NIfTI conversion is a three-step interactive flow:

1. Upload a ``.zip`` of DICOM files → server stages it and returns a staging_id.
2. Client fetches the series list (lightweight pydicom header reads, no conversion).
3. Client submits the confirmed series → modality mapping to trigger a conversion
   job (dcm2niix running as a container). Returns a run_id for status polling.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile

from app.auth.dependencies import CurrentUser, require_auth
from app.backends import get_backend
from app.backends.base import JobBackend
from app.config import Settings, get_settings
from app.models.dicom import (
    DicomConvertRequest,
    DicomConvertResult,
    DicomSeriesListing,
    DicomStagingResult,
)
from app.models.errors import ErrorDetail
from app.services import dicom_service, file_service, job_service

router = APIRouter(prefix="/projects/{project_id}/files/dicom", tags=["DICOM"])

_AUTH_ERRORS = {
    401: {"model": ErrorDetail, "description": "Missing or invalid token."},
    403: {"model": ErrorDetail, "description": "Access denied to this project."},
    404: {"model": ErrorDetail, "description": "Project or staging area not found."},
}


@router.post(
    "/upload",
    summary="Upload DICOM zip to staging",
    description=(
        "Accepts a single ``.zip`` archive of DICOM files. The archive is extracted "
        "with symlink and path-traversal checks into a private staging area. "
        "Use the returned ``staging_id`` to inspect series and submit a conversion job."
    ),
    response_model=DicomStagingResult,
    status_code=202,
    responses={**_AUTH_ERRORS, 400: {"model": ErrorDetail}},
)
async def upload_dicom(
    project_id: str,
    file: UploadFile,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> DicomStagingResult:
    pdir = file_service.resolve_project(settings, user, project_id)
    contents = await file.read()
    staging_id = dicom_service.stage_dicom_upload(pdir, contents, file.filename or "")
    return DicomStagingResult(staging_id=staging_id)


@router.get(
    "/{staging_id}/series",
    summary="Inspect DICOM series",
    description=(
        "Reads DICOM headers (via pydicom) in the staging area and returns a list of "
        "detected series with their description, modality, study date, and file count. "
        "No conversion happens at this step — it is safe to call multiple times."
    ),
    response_model=DicomSeriesListing,
    responses=_AUTH_ERRORS,
)
async def list_dicom_series(
    project_id: str,
    staging_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> DicomSeriesListing:
    pdir = file_service.resolve_project(settings, user, project_id)
    series = dicom_service.inspect_dicom_series(pdir, staging_id)
    return DicomSeriesListing(staging_id=staging_id, series=series)


@router.post(
    "/{staging_id}/convert",
    summary="Submit DICOM conversion job",
    description=(
        "Accepts a mapping of DICOM series UIDs to NiChart modality labels and submits "
        "a dcm2niix conversion job for each selected series. "
        "Series not listed are ignored. Returns a ``run_id`` for polling via "
        "``GET /jobs/pipelines/{run_id}``."
    ),
    response_model=DicomConvertResult,
    status_code=202,
    responses={**_AUTH_ERRORS, 400: {"model": ErrorDetail}},
)
async def convert_dicom(
    project_id: str,
    staging_id: str,
    body: DicomConvertRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    backend: JobBackend = Depends(get_backend),
) -> DicomConvertResult:
    if not body.series_mappings:
        raise HTTPException(400, "series_mappings must not be empty")

    pdir = file_service.resolve_project(settings, user, project_id)

    direct_steps: list[job_service.DirectStep] = []
    for mapping in body.series_mappings:
        series_dir = dicom_service.organize_series_files(pdir, staging_id, mapping.series_uid)

        mrid = mapping.mrid
        if not mrid:
            mrid = dicom_service.get_patient_id(pdir, staging_id, mapping.series_uid) or "subject"

        modality_dir = pdir / mapping.nichart_modality
        modality_dir.mkdir(parents=True, exist_ok=True)

        direct_steps.append(job_service.DirectStep(
            step_id=f"convert_{mapping.nichart_modality}_{mapping.series_uid[:8]}",
            tool_id="dcm2niix",
            mount_paths={
                "input": str(series_dir),
                "output": str(modality_dir),
            },
            params={"mrid": mrid},
        ))

    run = job_service.create_direct_run(
        user_id=user.sub,
        project_id=project_id,
        pipeline_id="dicom_convert",
        direct_steps=direct_steps,
    )
    background_tasks.add_task(
        job_service.run_direct_steps_task,
        run=run,
        direct_steps=direct_steps,
        backend=backend,
        user_token=user.token,
        tools_path=settings.tools_path,
        study_dir=pdir,
    )
    return DicomConvertResult(run_id=run.run_id)


@router.delete(
    "/{staging_id}",
    summary="Discard DICOM staging area",
    description=(
        "Permanently deletes the staged DICOM files without converting them. "
        "Staging areas are also auto-cleaned after ``NICHART_STAGING_TTL_HOURS``."
    ),
    status_code=204,
    responses=_AUTH_ERRORS,
)
async def discard_dicom_staging(
    project_id: str,
    staging_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    pdir = file_service.resolve_project(settings, user, project_id)
    dicom_service.discard_dicom_staging(pdir, staging_id)
