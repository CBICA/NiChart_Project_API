"""
Tests for file_service.sync_participants_with_images — the automatic creation and
extension of participants.csv after images are added to a project.

Plain service tests on a tmp_path project (no API client needed).
"""

import csv
from pathlib import Path

from app.services import file_service


def _add_images(project: Path, *mrids: str, modality: str = "t1") -> None:
    d = project / modality
    d.mkdir(parents=True, exist_ok=True)
    for mrid in mrids:
        (d / f"{mrid}.nii.gz").write_bytes(b"")


def _write_csv(project: Path, text: str) -> None:
    d = project / "participants"
    d.mkdir(parents=True, exist_ok=True)
    (d / "participants.csv").write_text(text)


def _read_csv(project: Path) -> tuple[list[str], list[dict]]:
    with (project / "participants" / "participants.csv").open(newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def test_no_images_is_a_noop(tmp_path):
    assert file_service.sync_participants_with_images(tmp_path) == 0
    assert not (tmp_path / "participants" / "participants.csv").exists()


def test_creates_file_with_defaults(tmp_path):
    _add_images(tmp_path, "sub02", "sub01")
    assert file_service.sync_participants_with_images(tmp_path) == 2
    cols, rows = _read_csv(tmp_path)
    assert cols == ["MRID", "Batch", "DX"]
    assert rows == [
        {"MRID": "sub01", "Batch": "Batch1", "DX": "CN"},
        {"MRID": "sub02", "Batch": "Batch1", "DX": "CN"},
    ]


def test_mrids_from_all_modalities_are_merged(tmp_path):
    _add_images(tmp_path, "sub01", modality="t1")
    _add_images(tmp_path, "sub01", "sub02", modality="fl")
    assert file_service.sync_participants_with_images(tmp_path) == 2
    _, rows = _read_csv(tmp_path)
    assert [r["MRID"] for r in rows] == ["sub01", "sub02"]


def test_appends_only_new_subjects_and_keeps_user_values(tmp_path):
    _write_csv(tmp_path, "MRID,Age,DX,Batch\nsub01,70,AD,SiteA\n")
    _add_images(tmp_path, "sub01", "sub02")
    assert file_service.sync_participants_with_images(tmp_path) == 1
    cols, rows = _read_csv(tmp_path)
    assert cols == ["MRID", "Age", "DX", "Batch"]  # column order untouched
    assert rows == [
        {"MRID": "sub01", "Age": "70", "DX": "AD", "Batch": "SiteA"},
        {"MRID": "sub02", "Age": "", "DX": "CN", "Batch": "Batch1"},
    ]


def test_defaults_only_for_columns_the_file_has(tmp_path):
    _write_csv(tmp_path, "MRID,Age\nsub01,70\n")
    _add_images(tmp_path, "sub01", "sub02")
    assert file_service.sync_participants_with_images(tmp_path) == 1
    cols, rows = _read_csv(tmp_path)
    assert cols == ["MRID", "Age"]
    assert rows[1] == {"MRID": "sub02", "Age": ""}


def test_rows_for_deleted_images_are_kept(tmp_path):
    _write_csv(tmp_path, "MRID,Batch,DX\ngone,Batch1,MCI\n")
    _add_images(tmp_path, "sub01")
    assert file_service.sync_participants_with_images(tmp_path) == 1
    _, rows = _read_csv(tmp_path)
    assert [r["MRID"] for r in rows] == ["gone", "sub01"]
    assert rows[0]["DX"] == "MCI"


def test_already_in_sync_does_not_rewrite(tmp_path):
    _write_csv(tmp_path, "MRID,Batch,DX\nsub01,Batch1,CN\n")
    _add_images(tmp_path, "sub01")
    before = (tmp_path / "participants" / "participants.csv").stat().st_mtime_ns
    assert file_service.sync_participants_with_images(tmp_path) == 0
    assert (tmp_path / "participants" / "participants.csv").stat().st_mtime_ns == before


def test_header_only_file_keeps_its_columns(tmp_path):
    _write_csv(tmp_path, "MRID,DX,Sex\n")
    _add_images(tmp_path, "sub01")
    assert file_service.sync_participants_with_images(tmp_path) == 1
    cols, rows = _read_csv(tmp_path)
    assert cols == ["MRID", "DX", "Sex"]
    assert rows == [{"MRID": "sub01", "DX": "CN", "Sex": ""}]


def test_lowercase_mrid_column_is_recognised(tmp_path):
    _write_csv(tmp_path, "mrid,Batch\nsub01,B2\n")
    _add_images(tmp_path, "sub01", "sub02")
    assert file_service.sync_participants_with_images(tmp_path) == 1
    cols, rows = _read_csv(tmp_path)
    assert cols == ["MRID", "Batch"]
    assert rows == [{"MRID": "sub01", "Batch": "B2"}, {"MRID": "sub02", "Batch": "Batch1"}]


def test_file_without_mrid_column_is_left_alone(tmp_path):
    original = "SubjectID,Age\ns1,70\n"
    _write_csv(tmp_path, original)
    _add_images(tmp_path, "sub01")
    assert file_service.sync_participants_with_images(tmp_path) == 0
    assert (tmp_path / "participants" / "participants.csv").read_text() == original
