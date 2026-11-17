"""
BIDS import (store_bids_upload / store_bids_files): scan IDs (MRIDs) that include the
session, and participants.tsv / sessions.tsv merged into participants.csv.

Plain service tests on tmp_path projects (no API client needed).
"""

import csv
import io
import zipfile
from pathlib import Path

from app.services import file_service


def _bids_zip(tmp_path: Path, files: dict[str, bytes | str]) -> Path:
    """Write a BIDS zip to disk and return its path (services take spooled paths)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    archive = tmp_path / "_spooled_upload.zip"
    archive.write_bytes(buf.getvalue())
    return archive


def _spool(tmp_path: Path, files: dict[str, bytes]) -> list[tuple[str, Path]]:
    """Write (relative_path, content) pairs to disk as the upload route would."""
    spool = tmp_path / "_spooled_files"
    spool.mkdir(exist_ok=True)
    out = []
    for i, (name, data) in enumerate(files.items()):
        dest = spool / str(i)
        dest.write_bytes(data)
        out.append((name, dest))
    return out


def _participants(project: Path) -> tuple[list[str], list[dict]]:
    with (project / "participants" / "participants.csv").open(newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def _images(project: Path, modality: str) -> list[str]:
    d = project / modality
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


_CAP = 64 * 1024 * 1024

NII = b"not-a-real-nifti"


def test_no_sessions_mrid_is_subject_label(tmp_path):
    z = _bids_zip(tmp_path, {
        "ds/sub-01/anat/sub-01_T1w.nii.gz": NII,
        "ds/sub-02/anat/sub-02_T1w.nii.gz": NII,
        "ds/participants.tsv": "participant_id\tage\tsex\nsub-01\t70\tM\nsub-02\t65\tF\n",
    })
    assert file_service.store_bids_upload(tmp_path, z, "ds.zip", max_extracted_bytes=_CAP) == 2
    assert _images(tmp_path, "t1") == ["01.nii.gz", "02.nii.gz"]
    cols, rows = _participants(tmp_path)
    assert cols == ["MRID", "Batch", "DX", "age", "sex"]
    assert rows == [
        {"MRID": "01", "Batch": "Batch1", "DX": "CN", "age": "70", "sex": "M"},
        {"MRID": "02", "Batch": "Batch1", "DX": "CN", "age": "65", "sex": "F"},
    ]


def test_sessions_are_part_of_the_mrid(tmp_path):
    z = _bids_zip(tmp_path, {
        "sub-01/ses-1/anat/sub-01_ses-1_T1w.nii.gz": NII,
        "sub-01/ses-2/anat/sub-01_ses-2_T1w.nii.gz": NII,
        "sub-01/ses-1/anat/sub-01_ses-1_FLAIR.nii.gz": NII,
        "participants.tsv": "participant_id\tsex\nsub-01\tM\n",
    })
    assert file_service.store_bids_upload(tmp_path, z, "ds.zip", max_extracted_bytes=_CAP) == 3
    assert _images(tmp_path, "t1") == ["01_ses-1.nii.gz", "01_ses-2.nii.gz"]
    assert _images(tmp_path, "fl") == ["01_ses-1.nii.gz"]
    _, rows = _participants(tmp_path)
    # one row per scan, each with the subject's values
    assert [(r["MRID"], r["sex"]) for r in rows] == [("01_ses-1", "M"), ("01_ses-2", "M")]


def test_sessions_tsv_adds_per_session_values(tmp_path):
    z = _bids_zip(tmp_path, {
        "sub-01/ses-1/anat/sub-01_ses-1_T1w.nii.gz": NII,
        "sub-01/ses-2/anat/sub-01_ses-2_T1w.nii.gz": NII,
        "participants.tsv": "participant_id\tsex\tage\nsub-01\tM\t\n",
        "sub-01/sub-01_sessions.tsv": "session_id\tage\nses-1\t70.5\nses-2\t72.1\n",
    })
    file_service.store_bids_upload(tmp_path, z, "ds.zip", max_extracted_bytes=_CAP)
    _, rows = _participants(tmp_path)
    assert {r["MRID"]: r["age"] for r in rows} == {"01_ses-1": "70.5", "01_ses-2": "72.1"}
    assert all(r["sex"] == "M" for r in rows)


def test_existing_participants_are_kept_and_extended(tmp_path):
    d = tmp_path / "participants"
    d.mkdir()
    (d / "participants.csv").write_text("MRID,Batch,DX,Age\n01,SiteA,AD,70\n")
    z = _bids_zip(tmp_path, {
        "sub-01/anat/sub-01_T1w.nii.gz": NII,
        "sub-02/anat/sub-02_T1w.nii.gz": NII,
        "participants.tsv": "participant_id\tAge\tsex\nsub-01\t99\tM\nsub-02\t60\tF\n",
    })
    file_service.store_bids_upload(tmp_path, z, "ds.zip", max_extracted_bytes=_CAP)
    cols, rows = _participants(tmp_path)
    assert cols == ["MRID", "Batch", "DX", "Age", "sex"]
    assert rows[0] == {"MRID": "01", "Batch": "SiteA", "DX": "AD", "Age": "70", "sex": ""}  # untouched
    assert rows[1] == {"MRID": "02", "Batch": "Batch1", "DX": "CN", "Age": "60", "sex": "F"}


def test_file_without_mrid_column_is_left_alone(tmp_path):
    d = tmp_path / "participants"
    d.mkdir()
    original = "SubjectID,Age\ns1,70\n"
    (d / "participants.csv").write_text(original)
    z = _bids_zip(tmp_path, {"sub-01/anat/sub-01_T1w.nii.gz": NII,
                   "participants.tsv": "participant_id\nsub-01\n"})
    file_service.store_bids_upload(tmp_path, z, "ds.zip", max_extracted_bytes=_CAP)
    assert (d / "participants.csv").read_text() == original


def test_files_upload_matches_zip(tmp_path):
    n = file_service.store_bids_files(tmp_path, _spool(tmp_path, {
        "ds/sub-03/ses-a/anat/sub-03_ses-a_T1w.nii.gz": NII,
        "ds/participants.tsv": b"participant_id\tsex\nsub-03\tF\n",
    }))
    assert n == 1
    assert _images(tmp_path, "t1") == ["03_ses-a.nii.gz"]
    _, rows = _participants(tmp_path)
    assert [(r["MRID"], r["sex"]) for r in rows] == [("03_ses-a", "F")]


def test_duplicate_runs_keep_first_and_non_bids_names_ignored(tmp_path):
    z = _bids_zip(tmp_path, {
        "sub-01/anat/sub-01_run-1_T1w.nii.gz": b"run1",
        "sub-01/anat/sub-01_run-2_T1w.nii.gz": b"run2",
        "extra/scan_T1w.nii.gz": NII,              # no sub- entity
        "sub-01/anat/sub-01_T1w.json": b"{}",       # sidecar, not an image
    })
    assert file_service.store_bids_upload(tmp_path, z, "ds.zip", max_extracted_bytes=_CAP) == 1
    assert (tmp_path / "t1" / "01.nii.gz").read_bytes() == b"run1"


def test_tsv_subject_without_images_keeps_a_row(tmp_path):
    z = _bids_zip(tmp_path, {
        "sub-01/anat/sub-01_T1w.nii.gz": NII,
        "participants.tsv": "participant_id\tsex\nsub-01\tM\nsub-09\tF\n",
    })
    file_service.store_bids_upload(tmp_path, z, "ds.zip", max_extracted_bytes=_CAP)
    _, rows = _participants(tmp_path)
    assert sorted(r["MRID"] for r in rows) == ["01", "09"]
