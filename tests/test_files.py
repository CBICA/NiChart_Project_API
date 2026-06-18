"""
Tests for file listing, download, delete, NIfTI staging, CSV/IDAT upload,
and the participants endpoints.
"""

import io
import zipfile

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_project(client, name="testproject"):
    resp = client.post("/projects", json={"name": name})
    assert resp.status_code == 201
    return name


# ── File listing ──────────────────────────────────────────────────────────────

def test_list_files_empty_project(data_client):
    pid = _create_project(data_client)
    resp = data_client.get(f"/projects/{pid}/files")
    assert resp.status_code == 200
    assert resp.json()["entries"] == []


def test_list_files_shows_content(data_client, tmp_path):
    pid = _create_project(data_client)
    # Write a file directly into the project dir
    user_dir = tmp_path / "LOCAL_USER" / pid
    (user_dir / "subdir").mkdir()
    (user_dir / "subdir" / "hello.txt").write_text("hello")
    resp = data_client.get(f"/projects/{pid}/files")
    assert resp.status_code == 200
    paths = [e["path"] for e in resp.json()["entries"]]
    assert "subdir" in paths
    assert "subdir/hello.txt" in paths


def test_list_files_excludes_hidden(data_client, tmp_path):
    pid = _create_project(data_client)
    user_dir = tmp_path / "LOCAL_USER" / pid
    (user_dir / "_working").mkdir()
    (user_dir / "_working" / "meta.json").write_text("{}")
    (user_dir / "_upload").mkdir()
    (user_dir / "_upload" / "tmp.nii").write_bytes(b"")
    (user_dir / "visible.txt").write_text("hi")
    resp = data_client.get(f"/projects/{pid}/files")
    paths = [e["path"] for e in resp.json()["entries"]]
    assert "visible.txt" in paths
    assert not any("_working" in p for p in paths)
    assert not any("_upload" in p for p in paths)


# ── Download ──────────────────────────────────────────────────────────────────

def test_download_single_file(data_client, tmp_path):
    pid = _create_project(data_client)
    (tmp_path / "LOCAL_USER" / pid / "data.txt").write_text("content")
    resp = data_client.get(f"/projects/{pid}/files/download?path=data.txt")
    assert resp.status_code == 200
    assert resp.content == b"content"


def test_download_directory_as_zip(data_client, tmp_path):
    pid = _create_project(data_client)
    d = tmp_path / "LOCAL_USER" / pid / "mydir"
    d.mkdir()
    (d / "a.txt").write_text("aaa")
    (d / "b.txt").write_text("bbb")
    resp = data_client.get(f"/projects/{pid}/files/download?path=mydir&zip=true")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    buf = io.BytesIO(resp.content)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
    assert "a.txt" in names
    assert "b.txt" in names


def test_download_path_traversal_rejected(data_client):
    pid = _create_project(data_client)
    resp = data_client.get(f"/projects/{pid}/files/download?path=../../etc/passwd")
    assert resp.status_code == 400


def test_download_missing_file(data_client):
    pid = _create_project(data_client)
    resp = data_client.get(f"/projects/{pid}/files/download?path=nope.txt")
    assert resp.status_code == 404


# ── Delete ────────────────────────────────────────────────────────────────────

def test_delete_file(data_client, tmp_path):
    pid = _create_project(data_client)
    f = tmp_path / "LOCAL_USER" / pid / "todel.txt"
    f.write_text("bye")
    resp = data_client.delete(f"/projects/{pid}/files?path=todel.txt")
    assert resp.status_code == 204
    assert not f.exists()


def test_delete_directory(data_client, tmp_path):
    pid = _create_project(data_client)
    d = tmp_path / "LOCAL_USER" / pid / "mydir"
    d.mkdir()
    (d / "f.txt").write_text("x")
    resp = data_client.delete(f"/projects/{pid}/files?path=mydir")
    assert resp.status_code == 204
    assert not d.exists()


def test_delete_path_traversal_rejected(data_client):
    pid = _create_project(data_client)
    resp = data_client.delete(f"/projects/{pid}/files?path=../../sensitive")
    assert resp.status_code == 400


# ── Participants ──────────────────────────────────────────────────────────────

def test_participants_empty(data_client):
    pid = _create_project(data_client)
    resp = data_client.get(f"/projects/{pid}/participants")
    assert resp.status_code == 200
    assert resp.json()["rows"] == []


def test_participants_patch_and_get(data_client):
    pid = _create_project(data_client)
    payload = {"rows": [
        {"MRID": "sub001", "Age": "45", "Sex": "M"},
        {"MRID": "sub002", "Age": "60", "Sex": "F"},
    ]}
    resp = data_client.patch(f"/projects/{pid}/participants", json=payload)
    assert resp.status_code == 204
    resp = data_client.get(f"/projects/{pid}/participants")
    assert resp.status_code == 200
    rows = {r["MRID"]: r for r in resp.json()["rows"]}
    assert rows["sub001"]["Age"] == "45"
    assert rows["sub002"]["Sex"] == "F"


def test_participants_patch_extra_columns(data_client):
    """Extra columns beyond MRID round-trip through the API unchanged."""
    pid = _create_project(data_client)
    payload = {"rows": [
        {"MRID": "sub001", "Age": "45", "Sex": "M", "MMSE": "28"},
    ]}
    resp = data_client.patch(f"/projects/{pid}/participants", json=payload)
    assert resp.status_code == 204
    resp = data_client.get(f"/projects/{pid}/participants")
    rows = resp.json()["rows"]
    assert rows[0]["MMSE"] == "28"


def test_participants_patch_replaces(data_client):
    pid = _create_project(data_client)
    data_client.patch(f"/projects/{pid}/participants", json={"rows": [
        {"MRID": "old", "Age": "30"},
    ]})
    data_client.patch(f"/projects/{pid}/participants", json={"rows": [
        {"MRID": "new", "Age": "25"},
    ]})
    resp = data_client.get(f"/projects/{pid}/participants")
    mrids = [r["MRID"] for r in resp.json()["rows"]]
    assert "new" in mrids
    assert "old" not in mrids


# ── CSV upload ────────────────────────────────────────────────────────────────

def test_csv_upload(data_client, tmp_path):
    pid = _create_project(data_client)
    csv_content = b"MRID,Age,Sex\nsub001,45,M\nsub002,60,F\n"
    resp = data_client.post(
        f"/projects/{pid}/files/upload/csv",
        files={"file": ("participants.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 204
    saved = (tmp_path / "LOCAL_USER" / pid / "participants" / "participants.csv").read_bytes()
    assert saved == csv_content


def test_csv_upload_wrong_extension(data_client):
    pid = _create_project(data_client)
    resp = data_client.post(
        f"/projects/{pid}/files/upload/csv",
        files={"file": ("data.xlsx", io.BytesIO(b"fake"), "application/octet-stream")},
    )
    assert resp.status_code == 400


# ── NIfTI staging ─────────────────────────────────────────────────────────────

def test_nifti_stage_and_commit(data_client, tmp_path):
    pid = _create_project(data_client)
    nifti_bytes = b"\x00" * 348  # minimal header placeholder
    resp = data_client.post(
        f"/projects/{pid}/files/upload/nifti",
        files=[("files", ("sub001_T1.nii.gz", io.BytesIO(nifti_bytes), "application/gzip"))],
    )
    assert resp.status_code == 202
    staging = resp.json()
    assert "staging_id" in staging
    assert len(staging["proposals"]) == 1
    proposal = staging["proposals"][0]
    assert proposal["inferred_mrid"] == "sub001"
    assert proposal["inferred_modality"] == "t1"

    # Commit
    resp = data_client.post(
        f"/projects/{pid}/files/stage/{staging['staging_id']}/commit",
        json={"mappings": [{"filename": "sub001_T1.nii.gz", "mrid": "sub001", "modality": "t1"}]},
    )
    assert resp.status_code == 200
    committed = resp.json()["committed"]
    assert len(committed) == 1
    assert committed[0]["mrid"] == "sub001"
    assert committed[0]["modality"] == "t1"
    assert (tmp_path / "LOCAL_USER" / pid / "t1" / "sub001.nii.gz").exists()


def test_nifti_stage_discard(data_client, tmp_path):
    pid = _create_project(data_client)
    resp = data_client.post(
        f"/projects/{pid}/files/upload/nifti",
        files=[("files", ("sub002_FL.nii", io.BytesIO(b"\x00" * 10), "application/octet-stream"))],
    )
    assert resp.status_code == 202
    staging_id = resp.json()["staging_id"]
    resp = data_client.delete(f"/projects/{pid}/files/stage/{staging_id}")
    assert resp.status_code == 204
    staging_dir = tmp_path / "LOCAL_USER" / pid / "_upload" / "nifti" / staging_id
    assert not staging_dir.exists()


def test_nifti_commit_not_found(data_client):
    pid = _create_project(data_client)
    resp = data_client.post(
        f"/projects/{pid}/files/stage/nonexistent-id/commit",
        json={"mappings": []},
    )
    assert resp.status_code == 404


def test_nifti_wrong_extension(data_client):
    pid = _create_project(data_client)
    resp = data_client.post(
        f"/projects/{pid}/files/upload/nifti",
        files=[("files", ("scan.dcm", io.BytesIO(b"\x00" * 10), "application/octet-stream"))],
    )
    assert resp.status_code == 400


# ── NIfTI modality inference ──────────────────────────────────────────────────

@pytest.mark.parametrize("filename,exp_mrid,exp_mod", [
    ("sub001_T1.nii.gz",   "sub001", "t1"),
    ("sub001_T1w.nii",     "sub001", "t1"),
    ("sub001_T1CE.nii.gz", "sub001", "t1ce"),
    ("sub001_FLAIR.nii",   "sub001", "fl"),
    ("sub001_FL.nii",      "sub001", "fl"),
    ("sub001_T2.nii",      "sub001", "t2"),
    ("sub001_ADC.nii",     "sub001", "adc"),
    ("unknown.nii.gz",     "unknown", None),
])
def test_nifti_inference(filename, exp_mrid, exp_mod):
    from app.services.file_service import infer_nifti_metadata
    mrid, mod = infer_nifti_metadata(filename)
    assert mrid == exp_mrid
    assert mod == exp_mod


# ── IDAT upload ───────────────────────────────────────────────────────────────

def test_idat_upload(data_client, tmp_path):
    pid = _create_project(data_client)
    # Build a zip with a .idat file
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("sample.idat", b"idat data")
    buf.seek(0)
    resp = data_client.post(
        f"/projects/{pid}/files/upload/idat",
        files={"file": ("data.zip", buf, "application/zip")},
    )
    assert resp.status_code == 204
    assert (tmp_path / "LOCAL_USER" / pid / "idat" / "sample.idat").exists()


def test_idat_upload_wrong_extension(data_client):
    pid = _create_project(data_client)
    resp = data_client.post(
        f"/projects/{pid}/files/upload/idat",
        files={"file": ("data.tar.gz", io.BytesIO(b"fake"), "application/gzip")},
    )
    assert resp.status_code == 400


# ── Readiness check ───────────────────────────────────────────────────────────

def test_readiness_no_requirements(data_client):
    """Pipeline with empty requires → always satisfied."""
    pid = _create_project(data_client)
    resp = data_client.get(f"/projects/{pid}/readiness/dummy_pipeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["satisfied"] is True
    assert body["imaging"] == []
    assert body["csv"] is None


def test_readiness_missing_pipeline(data_client):
    pid = _create_project(data_client)
    resp = data_client.get(f"/projects/{pid}/readiness/nonexistent_pipeline")
    assert resp.status_code == 404


def test_readiness_imaging_satisfied(data_client, tmp_path):
    """Imaging check passes when NIfTI files exist in the modality dir."""
    from app.services.readiness_service import check_readiness

    project_path = tmp_path / "LOCAL_USER" / "testproj"
    t1_dir = project_path / "t1"
    t1_dir.mkdir(parents=True)
    (t1_dir / "sub001.nii.gz").write_bytes(b"\x00" * 10)

    report = check_readiness(project_path, "fake_pipe", [{"needs_T1": None}, "needs_T1"])
    # "needs_T1" is the expected string form; the dict form is ignored
    assert any(c.modality == "t1" and c.satisfied for c in report.imaging)


def test_readiness_imaging_not_satisfied(data_client, tmp_path):
    """Imaging check fails when the modality dir is absent."""
    from app.services.readiness_service import check_readiness

    project_path = tmp_path / "LOCAL_USER" / "testproj"
    project_path.mkdir(parents=True)

    report = check_readiness(project_path, "fake_pipe", ["needs_T1"])
    assert len(report.imaging) == 1
    assert not report.imaging[0].satisfied
    assert not report.satisfied


def test_readiness_csv_columns_present(data_client, tmp_path):
    """CSV check passes when all required columns are populated."""
    from app.services.readiness_service import check_readiness

    project_path = tmp_path / "LOCAL_USER" / "testproj"
    csv_dir = project_path / "participants"
    csv_dir.mkdir(parents=True)
    (csv_dir / "participants.csv").write_text("MRID,Age,Sex\nsub001,65,M\nsub002,70,F\n")

    report = check_readiness(project_path, "fake_pipe", [{"csv_has_columns": ["Age", "Sex"]}])
    assert report.csv is not None
    assert report.csv.satisfied
    assert report.csv.total_subjects == 2


def test_readiness_csv_column_missing(data_client, tmp_path):
    """CSV check fails when a required column is absent from the file."""
    from app.services.readiness_service import check_readiness

    project_path = tmp_path / "LOCAL_USER" / "testproj"
    csv_dir = project_path / "participants"
    csv_dir.mkdir(parents=True)
    (csv_dir / "participants.csv").write_text("MRID,Age\nsub001,65\n")

    report = check_readiness(project_path, "fake_pipe", [{"csv_has_columns": ["Age", "MMSE"]}])
    assert report.csv is not None
    assert not report.csv.satisfied
    col_results = {c.column: c for c in report.csv.required_columns}
    assert col_results["Age"].present
    assert not col_results["MMSE"].present


def test_readiness_csv_subjects_missing_values(data_client, tmp_path):
    """CSV check reports which subjects are missing a required column value."""
    from app.services.readiness_service import check_readiness

    project_path = tmp_path / "LOCAL_USER" / "testproj"
    csv_dir = project_path / "participants"
    csv_dir.mkdir(parents=True)
    (csv_dir / "participants.csv").write_text("MRID,Age\nsub001,65\nsub002,\n")

    report = check_readiness(project_path, "fake_pipe", [{"csv_has_columns": ["Age"]}])
    assert report.csv is not None
    assert not report.csv.satisfied
    age_check = report.csv.required_columns[0]
    assert age_check.present
    assert "sub002" in age_check.subjects_missing


def test_readiness_via_api(data_client, tmp_path):
    """End-to-end readiness check via the HTTP endpoint."""
    pid = _create_project(data_client)
    # Write a participants.csv with Age and Sex columns
    csv_content = b"MRID,Age,Sex\nsub001,65,M\n"
    data_client.post(
        f"/projects/{pid}/files/upload/csv",
        files={"file": ("participants.csv", io.BytesIO(csv_content), "text/csv")},
    )
    # dummy_pipeline has no requirements — should be satisfied regardless
    resp = data_client.get(f"/projects/{pid}/readiness/dummy_pipeline")
    assert resp.status_code == 200
    assert resp.json()["satisfied"] is True
