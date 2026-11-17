"""
File management within a project — authenticated.

All paths from the client are validated against the project root before any I/O.
"""

import asyncio
import json
import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File as FastAPIFile, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from app.auth.dependencies import CurrentUser, require_auth
from app.config import Settings, get_settings
from app.models.errors import ErrorDetail
from app.models.files import (
    ArchiveLink,
    ArchiveLinkRequest,
    DeleteSubjectsRequest,
    DeleteSubjectsResult,
    DirectoryTree,
    DownloadZipRequest,
    NiftiCommitRequest,
    BidsUploadResult,
    NiftiCommitResult,
    NiftiStagingResult,
    ParticipantsList,
    ParticipantsUpdate,
)
from app.models.provenance import ProvenanceDossier, ProvenanceReport
from app.models.readiness import ReadinessReport
from app.services import (
    catalog_service,
    file_service,
    ingest_log,
    provenance_report,
    provenance_service,
    readiness_service,
    retention_service,
    upload_spool,
    usage_service,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["Files"])

_AUTH_ERRORS = {
    401: {"model": ErrorDetail, "description": "Missing or invalid token."},
    403: {"model": ErrorDetail, "description": "Access denied to this project."},
    404: {"model": ErrorDetail, "description": "Project or file not found."},
    400: {"model": ErrorDetail, "description": "Invalid or unsafe path."},
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
        return StreamingResponse(
            file_service.zip_directory_stream(target, project_path=pdir),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{target.name}.zip"'},
        )

    return FileResponse(path=target, filename=target.name)


@router.get(
    "/files/content",
    summary="Get a file or directory (same as /files/download)",
    description=(
        "Identical to ``GET /files/download``: a single file, or a directory as a streaming "
        "zip archive when ``zip=true``. Named to avoid 'download' in the URL, which some "
        "ad-blocker/privacy extensions filter — the frontend uses this one for reads made "
        "from script (chart data, the MRI viewer, previews). ``/files/download`` stays for "
        "existing links."
    ),
    response_model=None,
    responses={
        200: {"description": "File content or zip stream."},
        **_AUTH_ERRORS,
    },
)
async def get_file_content(
    project_id: str,
    path: str = Query(description="Path relative to the project root."),
    zip: bool = Query(default=False, description="Stream target directory as a zip archive."),
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse | FileResponse:
    return await download_file(project_id=project_id, path=path, zip=zip, user=user, settings=settings)


@router.post(
    "/files/archive",
    summary="Bundle a multi-file/directory selection into one zip",
    description=(
        "Bundles an arbitrary set of files and/or directories into a single zip archive, "
        "preserving each entry's path relative to the project root. Useful for downloading "
        "a multi-selection from the file browser as one archive instead of one request per item. "
        "Named to avoid 'download' in the URL, which some ad-blocker/privacy extensions filter."
    ),
    response_model=None,
    responses={
        200: {"description": "Zip stream."},
        **_AUTH_ERRORS,
    },
)
async def create_archive(
    project_id: str,
    body: DownloadZipRequest,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    pdir = file_service.resolve_project(settings, user, project_id)
    return StreamingResponse(
        file_service.zip_paths_stream(pdir, body.paths),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project_id}.zip"'},
    )


# ── Archive links (large selections, saved by the browser) ───────────────────

ARCHIVE_LINK_TTL = timedelta(minutes=15)


def _archive_links_dir(settings: Settings) -> Path:
    return Path(settings.data_root) / "_working" / "archive_links"


@router.post(
    "/files/archive/link",
    summary="Make a short-lived link that streams a selection as a zip",
    description=(
        "Same selection as ``POST /files/archive``, but instead of the zip it returns a URL "
        "(valid for 15 minutes, for this user and project only) that the browser can open "
        "itself, so a large archive streams straight to disk. Also says how many files and "
        "bytes the archive will hold, so the size can be shown before starting."
    ),
    response_model=ArchiveLink,
    responses=_AUTH_ERRORS,
)
async def create_archive_link(
    project_id: str,
    body: ArchiveLinkRequest,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> ArchiveLink:
    pdir = file_service.resolve_project(settings, user, project_id)
    count, total = file_service.archive_totals(pdir, body.paths)  # validates every path
    now = datetime.now(timezone.utc)
    links = _archive_links_dir(settings)
    links.mkdir(parents=True, exist_ok=True)
    for old in links.glob("*.json"):  # drop expired links
        try:
            if datetime.fromisoformat(json.loads(old.read_text())["expires_at"]) < now:
                old.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError):
            old.unlink(missing_ok=True)
    token = secrets.token_urlsafe(24)
    expires = now + ARCHIVE_LINK_TTL
    (links / f"{token}.json").write_text(json.dumps({
        "user": user.sub, "project": project_id, "paths": body.paths,
        "filename": body.filename or project_id, "expires_at": expires.isoformat(),
    }))
    return ArchiveLink(
        url=f"/projects/{quote(project_id)}/files/archive/{token}",
        file_count=count, total_bytes=total, expires_at=expires,
    )


@router.get(
    "/files/archive/{token}",
    summary="Stream a selection made with POST /files/archive/link",
    response_model=None,
    responses={200: {"description": "Zip stream."}, **_AUTH_ERRORS},
)
async def get_archive_link(
    project_id: str,
    token: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
        raise HTTPException(404, "This download link doesn't exist or has expired.")
    try:
        record = json.loads((_archive_links_dir(settings) / f"{token}.json").read_text())
        expired = datetime.fromisoformat(record["expires_at"]) < datetime.now(timezone.utc)
    except (OSError, ValueError, KeyError):
        raise HTTPException(404, "This download link doesn't exist or has expired.")
    if expired or record.get("user") != user.sub or record.get("project") != project_id:
        raise HTTPException(404, "This download link doesn't exist or has expired.")
    pdir = file_service.resolve_project(settings, user, project_id)
    return StreamingResponse(
        file_service.zip_paths_stream(pdir, record["paths"]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{record["filename"]}.zip"'},
    )


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
    if target == pdir.resolve():
        # "" or "." would rmtree the project behind the project API's back.
        raise HTTPException(400, "Can't delete the project folder itself; delete the project instead.")
    rel = target.relative_to(pdir.resolve()).as_posix()
    is_dir = target.is_dir()
    # Only deletions of input data matter to the ingest log; it ignores the rest.
    with ingest_log.tracked(pdir, "file_delete", {"path": path}):
        if is_dir:
            shutil.rmtree(target)
        else:
            target.unlink()
    # In cloud mode the S3 copy must go too, or the next run's sync down restores it.
    await file_service.unpublish_path(settings, user, project_id, rel, is_dir=is_dir)


@router.post(
    "/files/subjects/delete",
    summary="Remove subjects",
    description=(
        "Permanently deletes the listed subjects' images in every modality directory "
        "(``{modality}/{MRID}.nii[.gz]``) and, with ``remove_rows``, their rows in "
        "participants.csv — e.g. to keep only the subjects whose participant data is "
        "complete. Pipeline outputs already produced for them are left in place. With "
        "``dry_run``, only reports what would be deleted."
    ),
    response_model=DeleteSubjectsResult,
    responses=_AUTH_ERRORS,
)
async def delete_subjects(
    project_id: str,
    body: DeleteSubjectsRequest,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> DeleteSubjectsResult:
    pdir = file_service.resolve_project(settings, user, project_id)
    if body.dry_run:
        return file_service.delete_subjects(pdir, body.mrids, body.remove_rows, dry_run=True)
    with ingest_log.tracked(pdir, "subjects_delete", {"subjects": len(body.mrids)}):
        result = await asyncio.to_thread(
            file_service.delete_subjects, pdir, body.mrids, body.remove_rows
        )
    await file_service.unpublish_files(settings, user, project_id, result.files)
    if result.removed_rows:
        await file_service.publish_uploads(settings, user, project_id, pdir)
    return result


# ── NIfTI upload — stage ──────────────────────────────────────────────────────

@router.post(
    "/files/upload/nifti",
    summary="Upload NIfTI file(s) to staging",
    description=(
        "Accepts one or more ``.nii`` / ``.nii.gz`` files (including a folder upload, where "
        "filenames carry relative directory paths). Files land in the project staging area "
        "and the server returns its best-effort MRID and modality inference. If two entries "
        "flatten to the same filename, the first one wins and the rest are reported in "
        "``skipped_duplicates``. Follow up with the commit endpoint to move them into the "
        "project."
    ),
    response_model=NiftiStagingResult,
    status_code=202,
    responses=_UPLOAD_ERRORS,
)
async def upload_nifti(
    project_id: str,
    files: list[UploadFile],
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> NiftiStagingResult:
    pdir = file_service.resolve_project(settings, user, project_id)
    filenames = [f.filename or "" for f in files]
    with upload_spool.spool_dir(pdir) as spool:
        budget = upload_spool.UploadBudget(settings)
        sources = []
        for i, f in enumerate(files):
            dest = spool / f"{i}"
            await budget.spool(f, dest)
            sources.append(dest)
        usage_service.record_upload(
            "nifti", settings.execution_mode,
            files=len(sources), total_bytes=budget.total,
        )
        result = file_service.stage_nifti_files(pdir, filenames, sources)
    # Staged files are excluded from the S3 sync, so this is the one upload path
    # that does not go through publish_uploads and has to refresh the retention
    # countdown itself. Uploading is use, whether or not it has been committed yet.
    await retention_service.touch_project(
        settings.s3_data_bucket, settings.s3_data_prefix,
        user.sub, project_id, activity="upload",
    )
    return result


# ── NIfTI upload — zip ───────────────────────────────────────────────────────

@router.post(
    "/files/upload/nifti/zip",
    summary="Upload a zip of NIfTI files to staging",
    description=(
        "Accepts a ``.zip`` archive containing ``.nii`` / ``.nii.gz`` files "
        "(subdirectories are searched recursively; non-NIfTI entries are ignored). "
        "Files land in the project staging area and the server returns its best-effort "
        "MRID and modality inference. Directory components in the archive path contribute "
        "to modality detection (e.g. ``fl/subject001.nii.gz`` infers modality ``fl``). "
        "Nested directories are flattened into a single staging folder; if two entries "
        "flatten to the same filename, the first one wins and the rest are reported in "
        "``skipped_duplicates``. Follow up with the commit endpoint to move them into the "
        "project."
    ),
    response_model=NiftiStagingResult,
    status_code=202,
    responses=_UPLOAD_ERRORS,
)
async def upload_nifti_zip(
    project_id: str,
    file: UploadFile,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> NiftiStagingResult:
    pdir = file_service.resolve_project(settings, user, project_id)
    with upload_spool.spool_dir(pdir) as spool:
        archive = spool / "upload.zip"
        size = await upload_spool.UploadBudget(settings).spool(file, archive)
        usage_service.record_upload("nifti", settings.execution_mode, files=1, total_bytes=size)
        result = file_service.stage_nifti_zip(
            pdir, archive, file.filename or "",
            max_extracted_bytes=upload_spool.extracted_cap(settings),
        )
    await retention_service.touch_project(  # see upload_nifti
        settings.s3_data_bucket, settings.s3_data_prefix,
        user.sub, project_id, activity="upload",
    )
    return result


# ── NIfTI upload — commit ─────────────────────────────────────────────────────

@router.post(
    "/files/stage/{staging_id}/commit",
    summary="Commit staged NIfTI files",
    description=(
        "Confirms MRID and modality mappings for staged files and moves them into "
        "their final locations. Every staged file must appear in ``mappings``. If a "
        "subject already has a file at the target modality/MRID slot, the new upload "
        "is left in place there and the mapping is reported in ``skipped`` instead of "
        "``committed`` — existing scans are never overwritten by a re-upload."
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
    with ingest_log.tracked(pdir, "nifti_upload", {"staging_id": staging_id}):
        # In a thread: uncompressed uploads are gzipped on commit, which takes a while
        # for a large batch and mustn't block the event loop.
        result = await asyncio.to_thread(
            file_service.commit_nifti_staging, pdir, staging_id, body.mappings,
        )
        had_participants = file_service.participants_csv_exists(pdir)
        result.participants_added = file_service.sync_participants_with_images(pdir)
    result.participants_created = not had_participants and result.participants_added > 0
    await file_service.publish_uploads(settings, user, project_id, pdir)
    return result


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
    responses=_UPLOAD_ERRORS,
)
async def upload_csv(
    project_id: str,
    file: UploadFile,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    pdir = file_service.resolve_project(settings, user, project_id)
    with upload_spool.spool_dir(pdir) as spool:
        source = spool / "participants.csv"
        size = await upload_spool.UploadBudget(settings).spool(file, source)
        usage_service.record_upload("csv", settings.execution_mode, files=1, total_bytes=size)
        with ingest_log.tracked(pdir, "participants_upload", {"filename": file.filename}):
            file_service.store_csv_upload(pdir, source, file.filename or "")
    await file_service.publish_uploads(settings, user, project_id, pdir)


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
    response_model=BidsUploadResult,
    responses=_UPLOAD_ERRORS,
)
async def upload_bids(
    project_id: str,
    file: UploadFile,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> BidsUploadResult:
    pdir = file_service.resolve_project(settings, user, project_id)
    with upload_spool.spool_dir(pdir) as spool:
        archive = spool / "upload.zip"
        size = await upload_spool.UploadBudget(settings).spool(file, archive)
        usage_service.record_upload("bids", settings.execution_mode, files=1, total_bytes=size)
        with ingest_log.tracked(pdir, "bids_upload", {"filename": file.filename}):
            result = _import_bids(
                pdir,
                lambda: file_service.store_bids_upload(
                    pdir, archive, file.filename or "",
                    max_extracted_bytes=upload_spool.extracted_cap(settings),
                ),
            )
    await file_service.publish_uploads(settings, user, project_id, pdir)
    return result


@router.post(
    "/files/upload/bids/files",
    summary="Upload individual BIDS files",
    description=(
        "Accepts one or more files as multipart form data representing a BIDS dataset. "
        "Filenames may include relative sub-paths (e.g. ``sub-01/anat/sub-01_T1w.nii.gz``) "
        "and are validated for path traversal before writing. "
        "Reorganisation into the NiChart project layout is identical to the ZIP upload."
    ),
    status_code=202,
    response_model=BidsUploadResult,
    responses=_UPLOAD_ERRORS,
)
async def upload_bids_files(
    project_id: str,
    files: list[UploadFile] = FastAPIFile(...),
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> BidsUploadResult:
    pdir = file_service.resolve_project(settings, user, project_id)
    with upload_spool.spool_dir(pdir) as spool:
        budget = upload_spool.UploadBudget(settings)
        pairs = []
        for i, f in enumerate(files):
            dest = spool / f"{i}"
            await budget.spool(f, dest)
            pairs.append((f.filename or "", dest))
        usage_service.record_upload(
            "bids", settings.execution_mode,
            files=len(pairs), total_bytes=budget.total,
        )
        with ingest_log.tracked(pdir, "bids_upload", {"files": len(pairs)}):
            result = _import_bids(pdir, lambda: file_service.store_bids_files(pdir, pairs))
    await file_service.publish_uploads(settings, user, project_id, pdir)
    return result


def _import_bids(pdir: Path, store: Callable[[], int]) -> BidsUploadResult:
    """Run a BIDS import (``store`` returns the images placed), then add participants.csv
    rows for any imaged subjects it still lacks, and report what changed.

    Publishing is left to the caller rather than done here: this runs inside the
    upload's spool and ingest-log contexts, and an S3 publish should not hold the
    spooled archive on disk for its duration.
    """
    had_participants = file_service.participants_csv_exists(pdir)
    rows_before = file_service.participants_row_count(pdir)
    scans = store()
    file_service.sync_participants_with_images(pdir)
    added = max(0, file_service.participants_row_count(pdir) - rows_before)  # rows are never removed
    return BidsUploadResult(
        scans_imported=scans,
        participants_added=added,
        participants_created=not had_participants and file_service.participants_csv_exists(pdir),
    )


# ── IDAT upload ───────────────────────────────────────────────────────────────

@router.post(
    "/files/upload/idat",
    summary="Upload IDAT files",
    description="Accepts a ``.zip`` of ``.idat`` files. Extracted to ``idat/`` in the project.",
    status_code=204,
    responses=_UPLOAD_ERRORS,
)
async def upload_idat(
    project_id: str,
    file: UploadFile,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    pdir = file_service.resolve_project(settings, user, project_id)
    with upload_spool.spool_dir(pdir) as spool:
        archive = spool / "upload.zip"
        size = await upload_spool.UploadBudget(settings).spool(file, archive)
        usage_service.record_upload("idat", settings.execution_mode, files=1, total_bytes=size)
        with ingest_log.tracked(pdir, "idat_upload", {"filename": file.filename}):
            file_service.store_idat_upload(
                pdir, archive, file.filename or "",
                max_extracted_bytes=upload_spool.extracted_cap(settings),
            )
    await file_service.publish_uploads(settings, user, project_id, pdir)


@router.post(
    "/files/upload/idat/files",
    summary="Upload individual IDAT files",
    description=(
        "Accepts one or more ``.idat`` files as multipart form data. "
        "Filenames must be flat (no directory separators) and end with ``.idat``. "
        "Each filename is validated before any file is written to ``idat/``."
    ),
    status_code=204,
    responses=_UPLOAD_ERRORS,
)
async def upload_idat_files(
    project_id: str,
    files: list[UploadFile] = FastAPIFile(...),
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> None:
    pdir = file_service.resolve_project(settings, user, project_id)
    with upload_spool.spool_dir(pdir) as spool:
        budget = upload_spool.UploadBudget(settings)
        pairs = []
        for i, f in enumerate(files):
            dest = spool / f"{i}"
            await budget.spool(f, dest)
            pairs.append((f.filename or "", dest))
        usage_service.record_upload(
            "idat", settings.execution_mode,
            files=len(pairs), total_bytes=budget.total,
        )
        with ingest_log.tracked(pdir, "idat_upload", {"files": len(pairs)}):
            file_service.store_idat_files(pdir, pairs)
    await file_service.publish_uploads(settings, user, project_id, pdir)


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


@router.get(
    "/participants/template",
    summary="Download participants CSV template",
    description=(
        "Returns a CSV template pre-populated with all MRIDs detected from committed "
        "NIfTI files in the project's modality directories. Rows already present in "
        "participants.csv retain their values; newly detected MRIDs appear with empty "
        "additional columns. Use this as a starting point for filling in demographic or "
        "clinical data before uploading."
    ),
    responses={**_AUTH_ERRORS},
)
async def get_participants_template(
    project_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    pdir = file_service.resolve_project(settings, user, project_id)
    csv_content = file_service.participants_template_csv(pdir)
    return StreamingResponse(
        content=iter([csv_content.encode()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{project_id}_participants_template.csv"'},
    )


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
    with ingest_log.tracked(pdir, "participants_edit"):
        file_service.write_participants(pdir, body.rows)
    await file_service.publish_uploads(settings, user, project_id, pdir)


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


# ── Provenance verification ────────────────────────────────────────────────────

@router.get(
    "/provenance",
    summary="Verify step provenance",
    description=(
        "Scans all ``_provenance.json`` files written by the pipeline executor after each "
        "successful step, then checks whether any recorded input paths have been modified "
        "since the step finished. "
        "A 'dirty' entry means the step's cached result may be stale and the step should "
        "re-run. A 'missing_inputs' entry means a required input no longer exists. "
        "Returns 'no_provenance' summary when no steps have completed yet."
    ),
    response_model=ProvenanceReport,
    responses=_AUTH_ERRORS,
)
async def verify_provenance(
    project_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> ProvenanceReport:
    pdir = file_service.resolve_project(settings, user, project_id)
    return provenance_service.verify_provenance(pdir, project_id)


@router.get(
    "/provenance/report",
    summary="Project provenance report",
    description=(
        "How every current result of the project was produced: the raw inputs and every "
        "upload or edit of them (with timestamps and SHA-256 digests), each pipeline step "
        "that produced current outputs (run, image and digest, parameters, inputs and "
        "where they came from, outputs), the result → steps lineage, and shell commands "
        "to rerun each step with Docker or Singularity/Apptainer. Steps that ran before "
        "full provenance tracking are marked ``reconstructed``. Hashes are cached, but the "
        "first request on a large project reads every input and output file."
    ),
    response_model=ProvenanceDossier,
    responses=_AUTH_ERRORS,
)
async def provenance_report_endpoint(
    project_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> ProvenanceDossier:
    pdir = file_service.resolve_project(settings, user, project_id)
    return await asyncio.to_thread(
        provenance_report.build_dossier,
        pdir, project_id, user.sub,
        settings.pipelines_path, settings.tools_path, settings.resources_path,
    )


@router.get(
    "/provenance/checksums",
    summary="Checksums of inputs and current outputs",
    description=(
        "``sha256sum``-format list (``<sha256>  <project-relative path>``) of every raw input "
        "file and every file of the outputs in the provenance report. Check a copy of the "
        "project with ``sha256sum -c`` from its root."
    ),
    response_class=PlainTextResponse,
    responses=_AUTH_ERRORS,
)
async def provenance_checksums(
    project_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> PlainTextResponse:
    pdir = file_service.resolve_project(settings, user, project_id)
    text = await asyncio.to_thread(
        provenance_report.checksums,
        pdir, project_id, user.sub,
        settings.pipelines_path, settings.tools_path, settings.resources_path,
    )
    return PlainTextResponse(
        text,
        headers={"Content-Disposition": f'attachment; filename="{project_id}_checksums.sha256"'},
    )
