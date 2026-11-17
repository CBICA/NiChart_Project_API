"""
DICOM staging, series inspection, and per-series file organisation.

The actual dcm2niix conversion is submitted as a job via job_service; this
module handles the pure-Python DICOM side: staging uploads, reading headers,
and grouping files by SeriesInstanceUID ahead of the conversion job.
"""

import os
import shutil
import uuid
from collections import defaultdict
from pathlib import Path

import pydicom
from fastapi import HTTPException

from app.models.dicom import SeriesInfo
from app.services.path_security import (
    ArchiveTooLargeError,
    PathEscapeError,
    assert_safe_path,
    assert_safe_upload_filename,
    safe_unzip,
)

_STAGING_SUBDIR = Path("_upload") / "dicoms"


# ── Upload / staging ──────────────────────────────────────────────────────────

def stage_dicom_upload(
    project_path: Path, archive: Path, filename: str, *, max_extracted_bytes: int
) -> str:
    """Extract a staged DICOM zip into an isolated staging area. Returns the staging_id.

    *max_extracted_bytes* bounds the uncompressed contents; an archive over it
    raises ``HTTPException(413)``. DICOM pixel data is often already compressed,
    but a series of uncompressed slices is highly redundant, so this is the
    upload type where the bound matters most.
    """
    if not filename.lower().endswith(".zip"):
        raise HTTPException(400, "DICOM upload must be a .zip file")

    staging_id = uuid.uuid4().hex
    staging_dir = project_path / _STAGING_SUBDIR / staging_id
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        safe_unzip(archive, staging_dir, max_total_bytes=max_extracted_bytes)
    except ArchiveTooLargeError as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise HTTPException(413, str(exc))
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return staging_id


def stage_dicom_files(project_path: Path, files: list[tuple[str, Path]]) -> str:
    """Stage a batch of DICOM files supplied as (filename, spooled_path) pairs.

    Each filename is validated to be flat (no directory separators or traversal).
    Returns the staging_id exactly as ``stage_dicom_upload`` does.
    """
    staging_id = uuid.uuid4().hex
    staging_dir = project_path / _STAGING_SUBDIR / staging_id
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        for filename, source in files:
            assert_safe_upload_filename(filename, allow_subdirs=False)
            dest = staging_dir / Path(filename).name
            assert_safe_path(staging_dir, dest)
            # Scanner exports reuse names in every series folder (1-01.dcm, IM0001, …)
            # and a folder upload arrives flat: never let one file replace another.
            # Series are grouped by their headers, so the stored name doesn't matter.
            n = 1
            while dest.exists():
                dest = staging_dir / f"{n}_{Path(filename).name}"
                n += 1
            shutil.move(str(source), dest)
    except (PathEscapeError, Exception):
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return staging_id


def _resolve_staging(project_path: Path, staging_id: str) -> Path:
    d = project_path / _STAGING_SUBDIR / staging_id
    try:
        assert_safe_path(project_path, d)
    except PathEscapeError:
        raise HTTPException(400, "Invalid staging ID")
    if not d.is_dir():
        raise HTTPException(404, f"DICOM staging area '{staging_id}' not found")
    return d


# ── Series inspection ─────────────────────────────────────────────────────────

def inspect_dicom_series(project_path: Path, staging_id: str) -> list[SeriesInfo]:
    """Read DICOM headers in the staging area; return one SeriesInfo per series."""
    staging_dir = _resolve_staging(project_path, staging_id)

    series: dict[str, dict] = defaultdict(lambda: {
        "series_uid": None,
        "series_description": None,
        "modality": None,
        "study_date": None,
        "patient_id": None,
        "files": [],
    })

    for dcm_path in staging_dir.rglob("*"):
        if not dcm_path.is_file():
            continue
        try:
            ds = pydicom.dcmread(str(dcm_path), stop_before_pixels=True, force=True)
            uid = str(ds.get("SeriesInstanceUID", "")).strip()
            if not uid:
                continue
            s = series[uid]
            s["series_uid"] = uid
            s["files"].append(dcm_path)
            if not s["series_description"]:
                s["series_description"] = str(ds.get("SeriesDescription", "")).strip() or None
            if not s["modality"]:
                s["modality"] = str(ds.get("Modality", "")).strip() or None
            if not s["study_date"]:
                s["study_date"] = str(ds.get("StudyDate", "")).strip() or None
            if not s["patient_id"]:
                s["patient_id"] = str(ds.get("PatientID", "")).strip() or None
        except Exception:
            continue

    return [
        SeriesInfo(
            series_uid=s["series_uid"],
            series_description=s["series_description"],
            modality=s["modality"],
            study_date=s["study_date"],
            patient_id=s["patient_id"],
            num_files=len(s["files"]),
        )
        for s in series.values()
        if s["series_uid"]
    ]


# ── Series file organisation ──────────────────────────────────────────────────

def _link_or_copy(src: Path, dest: Path) -> None:
    """Place *src* at *dest* as a hardlink, falling back to a copy.

    The per-series subdirectory lives inside the same staging directory as
    *src*, so a hardlink is always on the same filesystem and costs no extra
    space. That matters: sorting a staging area into series used to leave a
    second full copy of every slice resident until the staging area was
    discarded, roughly doubling the disk a DICOM upload occupies. The
    conversion job sees an identical file either way, and removing the staging
    area removes both names.

    Some filesystems refuse hardlinks (and a bind-mounted volume may too), so a
    copy remains the fallback. An existing *dest* is replaced, matching the
    ``shutil.copy2`` behaviour this replaced.
    """
    if dest.exists():
        dest.unlink()
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def organize_series_files_bulk(
    project_path: Path,
    staging_id: str,
    series_uids: set[str],
) -> dict[str, tuple[Path, str | None]]:
    """
    Link DICOM files belonging to each of ``series_uids`` into series-specific
    subdirectories within the staging area (see :func:`_link_or_copy`, which
    hardlinks rather than copying), and capture each series' PatientID
    along the way. A single pass over the staging directory serves every
    requested series at once (rather than one full rescan per series), which
    matters once a staging area holds a large folder/zip upload with many
    series and files — this is also run off the event loop via
    ``asyncio.to_thread`` by the caller, since it's a blocking, potentially
    slow filesystem + DICOM-parsing operation.

    Returns ``{series_uid: (series_dir, patient_id)}``. ``series_dir`` is
    passed as the dcm2niix input mount; ``patient_id`` is the MRID fallback
    when the client didn't supply one.
    """
    staging_dir = _resolve_staging(project_path, staging_id)

    # The series UID arrives in the request body and becomes a directory name, so
    # it is contained before the first mkdir. A UID that resolves to the staging
    # directory itself ("", ".") is rejected too: it would make the "already
    # sorted into a series dir" test below skip every file, converting nothing.
    series_dirs = {uid: staging_dir / uid for uid in series_uids}
    for uid, d in series_dirs.items():
        try:
            assert_safe_path(staging_dir, d)
        except PathEscapeError:
            raise HTTPException(400, f"Invalid series UID {uid!r}")
        if d.resolve() == staging_dir.resolve():
            raise HTTPException(400, f"Invalid series UID {uid!r}")

    for d in series_dirs.values():
        d.mkdir(exist_ok=True)
    patient_ids: dict[str, str | None] = dict.fromkeys(series_uids)

    for dcm_path in staging_dir.rglob("*"):
        if not dcm_path.is_file():
            continue
        if any(dcm_path.is_relative_to(d) for d in series_dirs.values()):
            continue
        try:
            ds = pydicom.dcmread(str(dcm_path), stop_before_pixels=True, force=True)
            uid = str(ds.get("SeriesInstanceUID", "")).strip()
            if uid not in series_dirs:
                continue
            _link_or_copy(dcm_path, series_dirs[uid] / dcm_path.name)
            if patient_ids.get(uid) is None:
                pid = str(ds.get("PatientID", "")).strip()
                patient_ids[uid] = pid or None
        except Exception:
            continue

    return {uid: (series_dirs[uid], patient_ids[uid]) for uid in series_uids}


# ── Discard ───────────────────────────────────────────────────────────────────

def discard_dicom_staging(project_path: Path, staging_id: str) -> None:
    staging_dir = _resolve_staging(project_path, staging_id)
    shutil.rmtree(staging_dir)
