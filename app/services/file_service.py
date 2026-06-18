"""
File-system operations for projects and files within them.

All user-supplied paths are validated via path_security before any I/O.
"""

import csv
import io
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.auth.dependencies import CurrentUser
from app.config import Settings
from app.models.files import (
    CommittedFile,
    DirectoryTree,
    FileEntry,
    NiftiCommitResult,
    NiftiStagingResult,
    NiftiUploadProposal,
    ParticipantRow,
    ParticipantsList,
)
from app.models.projects import Project
from app.services.path_security import PathEscapeError, assert_safe_path, safe_unzip


# ── Project helpers ───────────────────────────────────────────────────────────

def user_root(settings: Settings, user: CurrentUser) -> Path:
    return settings.data_root / user.sub


def resolve_project(settings: Settings, user: CurrentUser, project_id: str) -> Path:
    root = user_root(settings, user)
    pdir = root / project_id
    try:
        assert_safe_path(root, pdir)
    except PathEscapeError:
        raise HTTPException(400, "Invalid project ID")
    if not pdir.is_dir():
        raise HTTPException(404, f"Project '{project_id}' not found")
    return pdir


# ── Project CRUD ──────────────────────────────────────────────────────────────

def list_projects(settings: Settings, user: CurrentUser) -> list[Project]:
    root = user_root(settings, user)
    if not root.exists():
        return []
    return [
        Project(id=d.name, created_at=datetime.fromtimestamp(d.stat().st_mtime))
        for d in sorted(root.iterdir())
        if d.is_dir()
    ]


def create_project(settings: Settings, user: CurrentUser, name: str) -> Project:
    root = user_root(settings, user)
    root.mkdir(parents=True, exist_ok=True)
    pdir = root / name
    if pdir.exists():
        raise HTTPException(409, f"Project '{name}' already exists")
    pdir.mkdir()
    return Project(id=name, created_at=datetime.fromtimestamp(pdir.stat().st_mtime))


def delete_project(settings: Settings, user: CurrentUser, project_id: str) -> None:
    pdir = resolve_project(settings, user, project_id)
    shutil.rmtree(pdir)


# ── File listing ──────────────────────────────────────────────────────────────

_HIDDEN = {"_upload", "_working"}


def list_project_files(project_path: Path) -> DirectoryTree:
    entries: list[FileEntry] = []
    for item in sorted(project_path.rglob("*")):
        rel_parts = item.relative_to(project_path).parts
        if any(p in _HIDDEN for p in rel_parts):
            continue
        stat = item.stat()
        entries.append(FileEntry(
            name=item.name,
            path=str(item.relative_to(project_path)),
            type="directory" if item.is_dir() else "file",
            size=stat.st_size if item.is_file() else None,
            mtime=stat.st_mtime,
        ))
    return DirectoryTree(entries=entries)


# ── File path resolution ──────────────────────────────────────────────────────

def resolve_file_path(project_path: Path, user_path: str) -> Path:
    """Resolve a user-supplied relative path within a project, checking containment."""
    target = (project_path / user_path).resolve()
    try:
        assert_safe_path(project_path, target)
    except PathEscapeError:
        raise HTTPException(400, "Path escapes project root")
    if not target.exists():
        raise HTTPException(404, f"'{user_path}' not found in project")
    return target


# ── Directory zip streaming ───────────────────────────────────────────────────

def zip_directory_bytes(directory: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(directory.rglob("*")):
            if item.is_file():
                zf.write(item, item.relative_to(directory))
    return buf.getvalue()


# ── Participants CSV ──────────────────────────────────────────────────────────

_PARTICIPANTS_PATH = Path("participants") / "participants.csv"


def read_participants(project_path: Path) -> ParticipantsList:
    csv_path = project_path / _PARTICIPANTS_PATH
    if not csv_path.exists():
        return ParticipantsList(rows=[])
    rows = []
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return ParticipantsList(rows=[])
        col = {k.lower(): k for k in reader.fieldnames}
        mrid_col = col.get("mrid")
        if not mrid_col:
            return ParticipantsList(rows=[])
        for raw_row in reader:
            mrid = (raw_row.get(mrid_col) or "").strip()
            if not mrid:
                continue
            row_dict: dict = {"MRID": mrid}
            for col_name, value in raw_row.items():
                if col_name == mrid_col:
                    continue
                row_dict[col_name] = value
            rows.append(ParticipantRow(**row_dict))
    return ParticipantsList(rows=rows)


def write_participants(project_path: Path, rows: list) -> None:
    """Write rows to participants.csv.

    Accepts either ``ParticipantRow`` instances or plain dicts. Every row must
    have an ``MRID`` key (case-insensitive on dicts). Column order is preserved
    from the first row; any row may have a subset of columns.
    """
    csv_dir = project_path / "participants"
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / "participants.csv"

    if not rows:
        with csv_path.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=["MRID"]).writeheader()
        return

    # Normalise each row to a dict, ensuring MRID is the canonical key.
    normalised: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            d: dict = {}
            mrid_val = None
            for k, v in row.items():
                if k.upper() == "MRID":
                    mrid_val = v
                else:
                    d[k] = v
            if mrid_val is None:
                raise ValueError(f"Row is missing required 'MRID' key: {row!r}")
            normalised.append({"MRID": mrid_val, **d})
        else:
            normalised.append(row.model_dump())

    # Collect column names in encounter order, MRID always first.
    seen: dict[str, None] = {"MRID": None}
    for d in normalised:
        for k in d:
            seen[k] = None
    fieldnames = list(seen)

    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for d in normalised:
            writer.writerow({k: ("" if d.get(k) is None else d.get(k, "")) for k in fieldnames})


# ── NIfTI filename inference ──────────────────────────────────────────────────

_MODALITY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("t1ce", re.compile(r"_T1CE", re.IGNORECASE)),
    ("fl",   re.compile(r"_(?:FLAIR|FL)\b", re.IGNORECASE)),
    ("t2",   re.compile(r"_T2\b", re.IGNORECASE)),
    ("adc",  re.compile(r"_ADC\b", re.IGNORECASE)),
    ("t1",   re.compile(r"_T1(?:w)?\b", re.IGNORECASE)),
]

_STRIP_SUFFIX = re.compile(
    r"(_T1CE|_FLAIR|_FL|_T2|_ADC|_T1w|_T1)?(\.nii\.gz|\.nii)$",
    re.IGNORECASE,
)


def infer_nifti_metadata(filename: str) -> tuple[str | None, str | None]:
    modality = None
    for mod, pattern in _MODALITY_PATTERNS:
        if pattern.search(filename):
            modality = mod
            break
    mrid = _STRIP_SUFFIX.sub("", filename).strip("_-. ")
    return mrid or None, modality


# ── NIfTI staging ─────────────────────────────────────────────────────────────

def stage_nifti_files(project_path: Path, filenames: list[str], file_data: list[bytes]) -> NiftiStagingResult:
    staging_id = str(uuid.uuid4())
    staging_dir = project_path / "_upload" / "nifti" / staging_id
    staging_dir.mkdir(parents=True, exist_ok=True)

    proposals = []
    for filename, data in zip(filenames, file_data):
        safe_name = Path(filename).name
        if not (safe_name.endswith(".nii") or safe_name.endswith(".nii.gz")):
            shutil.rmtree(staging_dir)
            raise HTTPException(400, f"'{filename}' is not a .nii or .nii.gz file")
        dest = staging_dir / safe_name
        assert_safe_path(staging_dir, dest)
        dest.write_bytes(data)
        mrid, modality = infer_nifti_metadata(safe_name)
        proposals.append(NiftiUploadProposal(
            filename=safe_name,
            inferred_mrid=mrid,
            inferred_modality=modality,
        ))

    return NiftiStagingResult(staging_id=staging_id, proposals=proposals)


def commit_nifti_staging(
    project_path: Path,
    staging_id: str,
    mappings: list,
) -> NiftiCommitResult:
    staging_dir = project_path / "_upload" / "nifti" / staging_id
    try:
        assert_safe_path(project_path, staging_dir)
    except PathEscapeError:
        raise HTTPException(400, "Invalid staging ID")
    if not staging_dir.is_dir():
        raise HTTPException(404, f"Staging area '{staging_id}' not found")

    committed = []
    for mapping in mappings:
        src = staging_dir / mapping.filename
        assert_safe_path(staging_dir, src)
        if not src.exists():
            raise HTTPException(404, f"Staged file '{mapping.filename}' not found")
        target_dir = project_path / mapping.modality
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{mapping.mrid}.nii.gz"
        shutil.move(str(src), target)
        committed.append(CommittedFile(
            mrid=mapping.mrid,
            modality=mapping.modality,
            path=str(target.relative_to(project_path)),
        ))

    # Clean up empty staging dir
    if staging_dir.exists() and not any(staging_dir.iterdir()):
        staging_dir.rmdir()

    return NiftiCommitResult(committed=committed)


def discard_nifti_staging(project_path: Path, staging_id: str) -> None:
    staging_dir = project_path / "_upload" / "nifti" / staging_id
    try:
        assert_safe_path(project_path, staging_dir)
    except PathEscapeError:
        raise HTTPException(400, "Invalid staging ID")
    if not staging_dir.is_dir():
        raise HTTPException(404, f"Staging area '{staging_id}' not found")
    shutil.rmtree(staging_dir)


# ── CSV upload ────────────────────────────────────────────────────────────────

def store_csv_upload(project_path: Path, contents: bytes, filename: str) -> None:
    if not filename.lower().endswith(".csv"):
        raise HTTPException(400, "Uploaded file must be a .csv")
    csv_dir = project_path / "participants"
    csv_dir.mkdir(parents=True, exist_ok=True)
    (csv_dir / "participants.csv").write_bytes(contents)


# ── BIDS upload ───────────────────────────────────────────────────────────────

# BIDS suffix → NiChart modality (t1ce must precede t1 to avoid false match)
_BIDS_MODALITY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("t1ce", re.compile(r"_T1CE", re.IGNORECASE)),
    ("fl",   re.compile(r"_(?:FLAIR|FL)\b", re.IGNORECASE)),
    ("t2",   re.compile(r"_T2w?\b", re.IGNORECASE)),   # handles both _T2 and BIDS _T2w
    ("adc",  re.compile(r"_ADC\b", re.IGNORECASE)),
    ("t1",   re.compile(r"_T1w?\b", re.IGNORECASE)),   # handles both _T1 and BIDS _T1w
]

_BIDS_SUB_RE = re.compile(r"^sub-(\w+)")


def store_bids_upload(project_path: Path, contents: bytes, filename: str) -> int:
    """
    Extract a BIDS zip and reorganise NIfTIs into the NiChart project layout.

    Returns the number of NIfTI files successfully placed.
    """
    if not filename.lower().endswith(".zip"):
        raise HTTPException(400, "BIDS upload must be a .zip file")

    committed = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_dir = Path(tmpdir)
        archive = tmp_dir / "upload.zip"
        archive.write_bytes(contents)
        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir()
        safe_unzip(archive, extract_dir)

        for nifti in extract_dir.rglob("*.nii.gz"):
            m = _BIDS_SUB_RE.match(nifti.name)
            if not m:
                continue
            mrid = m.group(1)
            modality = None
            for mod, pat in _BIDS_MODALITY_PATTERNS:
                if pat.search(nifti.name):
                    modality = mod
                    break
            if not modality:
                continue
            target_dir = project_path / modality
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(nifti, target_dir / f"{mrid}.nii.gz")
            committed += 1

        # Convert participants.tsv if present
        for tsv in extract_dir.rglob("participants.tsv"):
            _convert_bids_participants_tsv(tsv, project_path)
            break  # use only the top-level one

    return committed


def _convert_bids_participants_tsv(tsv_path: Path, project_path: Path) -> None:
    """Convert a BIDS participants.tsv to participants/participants.csv.

    Maps ``participant_id`` → ``MRID`` (stripping the ``sub-`` prefix).
    All other BIDS columns are preserved with their original names.
    """
    csv_dir = project_path / "participants"
    csv_dir.mkdir(parents=True, exist_ok=True)
    with tsv_path.open(newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    if not rows:
        return
    col = {k.lower(): k for k in fieldnames}
    pid_key = col.get("participant_id") or col.get("subject_id") or ""
    if not pid_key:
        return
    extra_cols = [k for k in fieldnames if k != pid_key]
    out_fieldnames = ["MRID"] + extra_cols
    out_path = csv_dir / "participants.csv"
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames)
        writer.writeheader()
        for row in rows:
            pid = (row.get(pid_key, "") or "").strip().removeprefix("sub-")
            if not pid:
                continue
            out_row: dict = {"MRID": pid}
            for c in extra_cols:
                out_row[c] = (row.get(c, "") or "").strip()
            writer.writerow(out_row)


# ── IDAT upload ───────────────────────────────────────────────────────────────

def store_idat_upload(project_path: Path, contents: bytes, filename: str) -> None:
    if not filename.lower().endswith(".zip"):
        raise HTTPException(400, "IDAT upload must be a .zip file")
    idat_dir = project_path / "idat"
    idat_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(contents)
        tmp_path = Path(tmp.name)
    try:
        safe_unzip(tmp_path, idat_dir)
    finally:
        tmp_path.unlink(missing_ok=True)
