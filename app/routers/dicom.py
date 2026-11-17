"""
DICOM staging, inspection, and conversion — authenticated.

DICOM → NIfTI conversion is a three-step interactive flow:

1. Upload a ``.zip`` of DICOM files → server stages it and returns a staging_id.
2. Client fetches the series list (lightweight pydicom header reads, no conversion).
3. Client submits the confirmed series → modality mapping to trigger a conversion
   job (dcm2niix running as a container). Returns a run_id for status polling.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from fastapi import File as FastAPIFile

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
from app.services import (
    dicom_service,
    file_service,
    ingest_log,
    job_service,
    upload_spool,
    usage_service,
)
from app.services.path_security import PathEscapeError, assert_safe_mrid

router = APIRouter(prefix="/projects/{project_id}/files/dicom", tags=["DICOM"])

_log = logging.getLogger(__name__)


async def _sync_participants_after_convert(
    settings: Settings, user: CurrentUser, project_id: str, pdir: Path, run_id: str,
    staging_id: str | None = None,
) -> None:
    """Add rows to participants.csv for subjects the conversion just produced.

    Queued after the conversion task; Starlette runs background tasks in order, so
    the converted NIfTIs are on disk by now. Only MRIDs that actually exist in the
    modality directories are added, so a failed conversion adds nothing. Errors are
    logged, not raised — there's no request left to report them to.
    """
    try:
        added = file_service.sync_participants_with_images(pdir)
        # The NIfTIs dcm2niix wrote and the rows just added, as one event; begin()
        # ran before the conversion was queued. run_id leads to the converter image.
        ingest_log.commit(pdir, "dicom_conversion", {"run_id": run_id, "tool_id": "dcm2niix"})
        if added:
            await file_service.publish_uploads(settings, user, project_id, pdir)
    except Exception:
        _log.exception("participants.csv sync after DICOM conversion failed for %s", project_id)
    # The original DICOMs stay in the hidden staging area, which the user can't see or
    # delete; once every series converted they're no longer needed. After a failure
    # they're kept so the conversion can be retried (the TTL sweep removes them later).
    if staging_id:
        try:
            if job_service.get_run_detail(run_id, user.sub).status == "succeeded":
                dicom_service.discard_dicom_staging(pdir, staging_id)
        except Exception:
            _log.exception("Removing DICOM staging %s after conversion failed", staging_id)

_AUTH_ERRORS = {
    401: {"model": ErrorDetail, "description": "Missing or invalid token."},
    403: {"model": ErrorDetail, "description": "Access denied to this project."},
    404: {"model": ErrorDetail, "description": "Project or staging area not found."},
}

_UPLOAD_ERRORS = {
    **_AUTH_ERRORS,
    413: {
        "model": ErrorDetail,
        "description": (
            "A file exceeds the per-file size limit, or the request exceeds the "
            "per-request limit. Nothing was saved. See GET /limits."
        ),
    },
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
    responses={**_UPLOAD_ERRORS, 400: {"model": ErrorDetail}},
)
async def upload_dicom(
    project_id: str,
    file: UploadFile,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> DicomStagingResult:
    pdir = file_service.resolve_project(settings, user, project_id)
    with upload_spool.spool_dir(pdir) as spool:
        archive = spool / "upload.zip"
        size = await upload_spool.UploadBudget(settings).spool(file, archive)
        usage_service.record_upload("dicom", settings.execution_mode, files=1, total_bytes=size)
        staging_id = dicom_service.stage_dicom_upload(
            pdir, archive, file.filename or "",
            max_extracted_bytes=upload_spool.extracted_cap(settings),
        )
    return DicomStagingResult(staging_id=staging_id)


@router.post(
    "/upload/files",
    summary="Upload individual DICOM files to staging",
    description=(
        "Accepts one or more raw DICOM files as multipart form data. "
        "Each filename is validated (no path traversal, no directory separators). "
        "Files are staged identically to the ZIP upload — use the returned "
        "``staging_id`` with the inspect and convert endpoints."
    ),
    response_model=DicomStagingResult,
    status_code=202,
    responses={**_UPLOAD_ERRORS, 400: {"model": ErrorDetail}},
)
async def upload_dicom_files(
    project_id: str,
    files: list[UploadFile] = FastAPIFile(...),
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> DicomStagingResult:
    pdir = file_service.resolve_project(settings, user, project_id)
    with upload_spool.spool_dir(pdir) as spool:
        budget = upload_spool.UploadBudget(settings)
        pairs = []
        for i, f in enumerate(files):
            dest = spool / f"{i}"
            await budget.spool(f, dest)
            pairs.append((f.filename or "", dest))
        usage_service.record_upload(
            "dicom", settings.execution_mode,
            files=len(pairs), total_bytes=budget.total,
        )
        staging_id = dicom_service.stage_dicom_files(pdir, pairs)
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
    series = await asyncio.to_thread(dicom_service.inspect_dicom_series, pdir, staging_id)
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

    # The MRID is substituted into the dcm2niix command line (``-f {mrid}``), so it
    # has to be a plain identifier. Checked before any work starts; the
    # header-derived fallback is checked below, once PatientID is known.
    for mapping in body.series_mappings:
        if mapping.mrid:
            try:
                assert_safe_mrid(mapping.mrid)
            except PathEscapeError as exc:
                raise HTTPException(400, str(exc))

    pdir = file_service.resolve_project(settings, user, project_id)

    # Organizing is a blocking filesystem + DICOM-header-parsing sweep over the whole
    # staging area; for a large folder/zip upload this can take long enough to stall
    # the event loop (and time out the request), so it runs off-thread.
    series_uids = {m.series_uid for m in body.series_mappings}
    ingest_log.begin(pdir)
    organized = await asyncio.to_thread(
        dicom_service.organize_series_files_bulk, pdir, staging_id, series_uids
    )

    direct_steps: list[job_service.DirectStep] = []
    targets: dict[tuple[str, str], str] = {}
    for mapping in body.series_mappings:
        series_dir, patient_id = organized[mapping.series_uid]
        mrid = mapping.mrid or patient_id or "subject"
        # Two series converted to the same MRID and modality would write one file.
        key = (mapping.nichart_modality, mrid)
        if key in targets:
            raise HTTPException(
                400,
                f"Series {targets[key]} and {mapping.series_uid} would both be saved as "
                f"{mapping.nichart_modality}/{mrid}.nii.gz. Give one of them a different "
                "'mrid', or convert only one.",
            )
        targets[key] = mapping.series_uid
        # PatientID comes from the DICOM header, so it is untrusted too.
        try:
            assert_safe_mrid(mrid)
        except PathEscapeError as exc:
            raise HTTPException(
                400,
                f"Series {mapping.series_uid}: {exc}. Supply an explicit 'mrid' for "
                "this series.",
            )

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
    usage_service.record_pipeline_run(
        "dicom_convert", settings.execution_mode, subjects=len(direct_steps)
    )
    background_tasks.add_task(
        job_service.run_direct_steps_task,
        run=run,
        direct_steps=direct_steps,
        backend=backend,
        user_token=user.token,
        tools_path=settings.tools_path,
        study_dir=pdir,
        execution_mode=settings.execution_mode,
    )
    background_tasks.add_task(
        _sync_participants_after_convert, settings, user, project_id, pdir, run.run_id, staging_id
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
