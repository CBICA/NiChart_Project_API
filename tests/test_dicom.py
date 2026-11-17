"""
Tests for the DICOM staging, series inspection, and conversion endpoints.

Real minimal DICOM files are created with pydicom for the inspection tests.
Conversion tests use MockBackend (via job_client fixture).
"""

import io
import zipfile

import pytest


# ── DICOM test-file factory ────────────────────────────────────────────────────

def make_dicom_zip(
    series_uid: str = "1.2.3.4.5",
    patient_id: str = "TEST001",
    modality: str = "MR",
) -> bytes:
    """Create an in-memory zip containing one minimal valid DICOM file."""
    import pydicom
    from pydicom.dataset import Dataset, FileDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\x00" * 128)
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = patient_id
    ds.Modality = modality
    ds.StudyDate = "20240101"
    ds.SeriesDescription = "T1w MPRAGE"

    dcm_buf = io.BytesIO()
    pydicom.dcmwrite(dcm_buf, ds)
    dcm_bytes = dcm_buf.getvalue()

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("series.dcm", dcm_bytes)
    zip_buf.seek(0)
    return zip_buf.read()


def make_plain_zip(filename: str = "test.dcm", content: bytes = b"not-dicom") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, content)
    buf.seek(0)
    return buf.read()


def _create_project(client, name="dicomtest"):
    resp = client.post("/projects", json={"name": name})
    assert resp.status_code == 201
    return name


@pytest.fixture(autouse=True)
def _clear_run_store():
    from app.services import job_service
    job_service._data_root = None
    yield
    job_service._data_root = None


# ── Upload ────────────────────────────────────────────────────────────────────

def test_upload_dicom_zip(job_client):
    pid = _create_project(job_client, "dcmupload")
    zip_bytes = make_plain_zip()
    resp = job_client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("dicoms.zip", io.BytesIO(zip_bytes), "application/zip")},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert "staging_id" in data
    assert data["staging_id"]


def test_upload_dicom_wrong_extension(job_client):
    pid = _create_project(job_client, "dcmbadext")
    resp = job_client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("dicoms.tar.gz", io.BytesIO(b"fake"), "application/gzip")},
    )
    assert resp.status_code == 400


# ── Series inspection ─────────────────────────────────────────────────────────

def test_inspect_series_no_valid_dicoms(job_client):
    """Non-DICOM files in the zip → empty series list (pydicom skips them)."""
    pid = _create_project(job_client, "dcminspect0")
    zip_bytes = make_plain_zip()
    staging_id = job_client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("d.zip", io.BytesIO(zip_bytes), "application/zip")},
    ).json()["staging_id"]

    resp = job_client.get(f"/projects/{pid}/files/dicom/{staging_id}/series")
    assert resp.status_code == 200
    data = resp.json()
    assert data["staging_id"] == staging_id
    assert isinstance(data["series"], list)


def test_inspect_series_with_real_dicom(job_client):
    """Real minimal DICOM → server detects the series correctly."""
    pid = _create_project(job_client, "dcminspect1")
    series_uid = "1.2.3.4.99"
    zip_bytes = make_dicom_zip(series_uid=series_uid, patient_id="SUB001")
    staging_id = job_client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("d.zip", io.BytesIO(zip_bytes), "application/zip")},
    ).json()["staging_id"]

    resp = job_client.get(f"/projects/{pid}/files/dicom/{staging_id}/series")
    assert resp.status_code == 200
    series_list = resp.json()["series"]
    assert len(series_list) >= 1
    uids = [s["series_uid"] for s in series_list]
    assert series_uid in uids
    matching = next(s for s in series_list if s["series_uid"] == series_uid)
    assert matching["patient_id"] == "SUB001"
    assert matching["modality"] == "MR"
    assert matching["num_files"] >= 1


def test_inspect_series_not_found(job_client):
    pid = _create_project(job_client, "dcminspect2")
    resp = job_client.get(f"/projects/{pid}/files/dicom/nonexistent-id/series")
    assert resp.status_code == 404


# ── Discard ───────────────────────────────────────────────────────────────────

def test_discard_staging(job_client, tmp_path):
    pid = _create_project(job_client, "dcmdiscard")
    zip_bytes = make_plain_zip()
    staging_id = job_client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("d.zip", io.BytesIO(zip_bytes), "application/zip")},
    ).json()["staging_id"]

    resp = job_client.delete(f"/projects/{pid}/files/dicom/{staging_id}")
    assert resp.status_code == 204

    # Staging dir should be gone
    staging_dir = tmp_path / "LOCAL_USER" / pid / "_upload" / "dicoms" / staging_id
    assert not staging_dir.exists()


def test_discard_staging_not_found(job_client):
    pid = _create_project(job_client, "dcmdiscard2")
    resp = job_client.delete(f"/projects/{pid}/files/dicom/nonexistent-id")
    assert resp.status_code == 404


# ── Convert (requires MockBackend) ─────────────────────────────────────────────

def test_convert_returns_run_id(job_client):
    pid = _create_project(job_client, "dcmconvert")
    series_uid = "1.2.3.4.77"
    zip_bytes = make_dicom_zip(series_uid=series_uid, patient_id="SUBJ01")
    staging_id = job_client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("d.zip", io.BytesIO(zip_bytes), "application/zip")},
    ).json()["staging_id"]

    resp = job_client.post(
        f"/projects/{pid}/files/dicom/{staging_id}/convert",
        json={"series_mappings": [{"series_uid": series_uid, "nichart_modality": "t1"}]},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert "run_id" in data


def test_convert_with_explicit_mrid(job_client):
    pid = _create_project(job_client, "dcmconvert2")
    series_uid = "1.2.3.4.88"
    zip_bytes = make_dicom_zip(series_uid=series_uid)
    staging_id = job_client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("d.zip", io.BytesIO(zip_bytes), "application/zip")},
    ).json()["staging_id"]

    resp = job_client.post(
        f"/projects/{pid}/files/dicom/{staging_id}/convert",
        json={"series_mappings": [
            {"series_uid": series_uid, "nichart_modality": "fl", "mrid": "custom_mrid"}
        ]},
    )
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    # Job should complete with MockBackend
    detail = job_client.get(f"/jobs/pipelines/{run_id}").json()
    assert detail["status"] == "succeeded"


@pytest.mark.parametrize("mrid", ["../../escaped", "site1/sub01", " sub001", "-f", "sub\\001"])
def test_convert_rejects_unsafe_mrid(job_client, mrid):
    """The MRID reaches the dcm2niix command line, so an unsafe one is a 400."""
    pid = _create_project(job_client, "dcmbadmrid")
    series_uid = "1.2.3.4.89"
    zip_bytes = make_dicom_zip(series_uid=series_uid)
    staging_id = job_client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("d.zip", io.BytesIO(zip_bytes), "application/zip")},
    ).json()["staging_id"]

    resp = job_client.post(
        f"/projects/{pid}/files/dicom/{staging_id}/convert",
        json={"series_mappings": [
            {"series_uid": series_uid, "nichart_modality": "t1", "mrid": mrid}
        ]},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("series_uid", [
    "../../escaped",        # traversal out of the staging area
    "../escaped",
    "/tmp/escaped",         # absolute: pathlib would discard the staging prefix
    "",                     # resolves to the staging dir itself
    ".",
])
def test_convert_rejects_unsafe_series_uid(job_client, tmp_path, series_uid):
    """The series UID becomes a directory name, so an unsafe one is a 400."""
    pid = _create_project(job_client, "dcmbaduid")
    zip_bytes = make_dicom_zip(series_uid="1.2.3.4.90")
    staging_id = job_client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("d.zip", io.BytesIO(zip_bytes), "application/zip")},
    ).json()["staging_id"]

    resp = job_client.post(
        f"/projects/{pid}/files/dicom/{staging_id}/convert",
        json={"series_mappings": [
            {"series_uid": series_uid, "nichart_modality": "t1", "mrid": "sub001"}
        ]},
    )
    assert resp.status_code == 400, resp.text
    assert not (tmp_path / "escaped").exists()


def test_convert_empty_mappings(job_client):
    pid = _create_project(job_client, "dcmempty")
    zip_bytes = make_plain_zip()
    staging_id = job_client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("d.zip", io.BytesIO(zip_bytes), "application/zip")},
    ).json()["staging_id"]

    resp = job_client.post(
        f"/projects/{pid}/files/dicom/{staging_id}/convert",
        json={"series_mappings": []},
    )
    assert resp.status_code == 400


# ── Series organisation: disk cost ────────────────────────────────────────────

def test_series_organisation_hardlinks_rather_than_copies(tmp_path):
    """Sorting into series must not leave a second full copy of every slice.

    The series subdirectory is inside the same staging directory as the source,
    so the link is always same-filesystem. Checked by inode identity and link
    count rather than by content, since a copy would pass a content check.
    """
    from app.services.dicom_service import organize_series_files_bulk

    series_uid = "1.2.3.4.555"
    staging_id = "stage1"
    staging = tmp_path / "_upload" / "dicoms" / staging_id
    staging.mkdir(parents=True)

    zip_bytes = make_dicom_zip(series_uid=series_uid, patient_id="SUBJ77")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(staging)
    source = staging / "series.dcm"
    assert source.stat().st_nlink == 1

    result = organize_series_files_bulk(tmp_path, staging_id, {series_uid})
    series_dir, patient_id = result[series_uid]
    assert patient_id == "SUBJ77"

    linked = series_dir / "series.dcm"
    assert linked.exists()
    assert linked.stat().st_ino == source.stat().st_ino
    assert linked.stat().st_nlink == 2
    # Both names, one allocation: the staging area has not doubled in size.
    assert linked.stat().st_blocks == source.stat().st_blocks


def test_series_organisation_falls_back_to_a_copy(tmp_path, monkeypatch):
    """Filesystems that refuse hardlinks must still get a usable series dir."""
    import os as _os

    from app.services import dicom_service

    series_uid = "1.2.3.4.556"
    staging_id = "stage2"
    staging = tmp_path / "_upload" / "dicoms" / staging_id
    staging.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(make_dicom_zip(series_uid=series_uid))) as zf:
        zf.extractall(staging)

    def _refuse(*args, **kwargs):
        raise OSError("hardlinks not supported here")

    monkeypatch.setattr(_os, "link", _refuse)

    result = dicom_service.organize_series_files_bulk(tmp_path, staging_id, {series_uid})
    linked = result[series_uid][0] / "series.dcm"
    assert linked.exists()
    assert linked.stat().st_ino != (staging / "series.dcm").stat().st_ino
