"""
DICOM staging, series inspection, and per-series file organisation.

The actual dcm2niix conversion is submitted as a job via job_service; this
module handles the pure-Python DICOM side: staging uploads, reading headers,
and grouping files by SeriesInstanceUID ahead of the conversion job.
"""

import shutil
import tempfile
import uuid
from collections import defaultdict
from pathlib import Path

import pydicom
from fastapi import HTTPException

from app.models.dicom import SeriesInfo
from app.services.path_security import PathEscapeError, assert_safe_path, safe_unzip

_STAGING_SUBDIR = Path("_upload") / "dicoms"


# ── Upload / staging ──────────────────────────────────────────────────────────

def stage_dicom_upload(project_path: Path, contents: bytes, filename: str) -> str:
    """Extract a DICOM zip into an isolated staging area. Returns the staging_id."""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(400, "DICOM upload must be a .zip file")

    staging_id = uuid.uuid4().hex
    staging_dir = project_path / _STAGING_SUBDIR / staging_id
    staging_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(contents)
        tmp_path = Path(tmp.name)

    try:
        safe_unzip(tmp_path, staging_dir)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    finally:
        tmp_path.unlink(missing_ok=True)

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

def organize_series_files(
    project_path: Path,
    staging_id: str,
    series_uid: str,
) -> Path:
    """
    Copy all DICOM files belonging to ``series_uid`` into a series-specific
    subdirectory within the staging area. Returns the subdirectory path.

    The subdirectory is passed as the dcm2niix input mount.
    """
    staging_dir = _resolve_staging(project_path, staging_id)
    series_dir = staging_dir / series_uid
    series_dir.mkdir(exist_ok=True)

    for dcm_path in staging_dir.rglob("*"):
        if not dcm_path.is_file():
            continue
        if dcm_path.is_relative_to(series_dir):
            continue
        try:
            ds = pydicom.dcmread(str(dcm_path), stop_before_pixels=True, force=True)
            if str(ds.get("SeriesInstanceUID", "")).strip() == series_uid:
                shutil.copy2(dcm_path, series_dir / dcm_path.name)
        except Exception:
            continue

    return series_dir


def get_patient_id(
    project_path: Path,
    staging_id: str,
    series_uid: str,
) -> str | None:
    """Return the PatientID tag from the first DICOM file matching series_uid."""
    staging_dir = _resolve_staging(project_path, staging_id)
    for dcm_path in staging_dir.rglob("*"):
        if not dcm_path.is_file():
            continue
        try:
            ds = pydicom.dcmread(str(dcm_path), stop_before_pixels=True, force=True)
            if str(ds.get("SeriesInstanceUID", "")).strip() == series_uid:
                pid = str(ds.get("PatientID", "")).strip()
                return pid or None
        except Exception:
            continue
    return None


# ── Discard ───────────────────────────────────────────────────────────────────

def discard_dicom_staging(project_path: Path, staging_id: str) -> None:
    staging_dir = _resolve_staging(project_path, staging_id)
    shutil.rmtree(staging_dir)
