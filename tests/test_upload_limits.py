"""
Tests for the upload size limits (per-file and per-request) and GET /limits.

Every ordinary upload endpoint streams to disk through
``app.services.upload_spool`` and shares one pair of caps, so each endpoint is
checked against both.
"""

import gzip
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.auth.dependencies import require_auth
from tests.conftest import _override_require_auth

_FILE_CAP_MB = 2
_REQUEST_CAP_MB = 3
_MIB = 1024 * 1024


@pytest.fixture
def capped_client(tmp_path):
    """Local-mode client with caps small enough to exceed in a test."""
    from app.config import Settings, get_settings
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        execution_mode="local",
        data_root=tmp_path,
        max_upload_file_mb=_FILE_CAP_MB,
        max_upload_request_mb=_REQUEST_CAP_MB,
    )
    app.dependency_overrides[require_auth] = _override_require_auth

    with TestClient(app, raise_server_exceptions=True) as client:
        yield client

    app.dependency_overrides.clear()


def _project(client, name="limits"):
    assert client.post("/projects", json={"name": name}).status_code == 201
    return name


def _blob(mib):
    return b"\x00" * int(mib * _MIB)


def _zip_of(name, payload):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    buf.seek(0)
    return buf.read()


# ── GET /limits ───────────────────────────────────────────────────────────────

def test_limits_is_public_and_reports_the_caps(capped_client):
    resp = capped_client.get("/limits")
    assert resp.status_code == 200
    assert resp.json() == {
        "max_upload_file_mb": _FILE_CAP_MB,
        "max_upload_request_mb": _REQUEST_CAP_MB,
    }


# ── Per-file cap ──────────────────────────────────────────────────────────────

# (endpoint suffix, field name, filename) for every ordinary upload route.
_SINGLE_FILE_ENDPOINTS = [
    ("/files/upload/nifti/zip", "file", "big.zip"),
    ("/files/upload/csv", "file", "big.csv"),
    ("/files/upload/bids", "file", "big.zip"),
    ("/files/upload/idat", "file", "big.zip"),
]

_MULTI_FILE_ENDPOINTS = [
    ("/files/upload/nifti", "files", "big.nii.gz"),
    ("/files/upload/bids/files", "files", "sub-01/anat/sub-01_T1w.nii.gz"),
    ("/files/upload/idat/files", "files", "big.idat"),
]


@pytest.mark.parametrize("suffix,field,name", _SINGLE_FILE_ENDPOINTS)
def test_single_file_endpoint_rejects_oversized_file(capped_client, tmp_path, suffix, field, name):
    pid = _project(capped_client)
    payload = _blob(_FILE_CAP_MB + 1)
    resp = capped_client.post(
        f"/projects/{pid}{suffix}",
        files={field: (name, io.BytesIO(payload), "application/octet-stream")},
    )
    assert resp.status_code == 413, resp.text
    assert str(_FILE_CAP_MB) in resp.json()["detail"]


@pytest.mark.parametrize("suffix,field,name", _MULTI_FILE_ENDPOINTS)
def test_multi_file_endpoint_rejects_oversized_file(capped_client, suffix, field, name):
    pid = _project(capped_client)
    payload = _blob(_FILE_CAP_MB + 1)
    resp = capped_client.post(
        f"/projects/{pid}{suffix}",
        files=[(field, (name, io.BytesIO(payload), "application/octet-stream"))],
    )
    assert resp.status_code == 413, resp.text
    assert str(_FILE_CAP_MB) in resp.json()["detail"]


def test_dicom_zip_rejects_oversized_file(capped_client):
    pid = _project(capped_client)
    resp = capped_client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("big.zip", io.BytesIO(_blob(_FILE_CAP_MB + 1)), "application/zip")},
    )
    assert resp.status_code == 413, resp.text


def test_dicom_files_rejects_oversized_file(capped_client):
    pid = _project(capped_client)
    resp = capped_client.post(
        f"/projects/{pid}/files/dicom/upload/files",
        files=[("files", ("big.dcm", io.BytesIO(_blob(_FILE_CAP_MB + 1)), "application/dicom"))],
    )
    assert resp.status_code == 413, resp.text


# ── Per-request cap ───────────────────────────────────────────────────────────

def test_request_cap_rejects_a_batch_that_is_individually_fine(capped_client):
    """Two files each under the per-file cap, together over the per-request cap."""
    pid = _project(capped_client)
    each = _blob(_FILE_CAP_MB)
    resp = capped_client.post(
        f"/projects/{pid}/files/upload/nifti",
        files=[
            ("files", ("a_T1.nii.gz", io.BytesIO(each), "application/gzip")),
            ("files", ("b_T1.nii.gz", io.BytesIO(each), "application/gzip")),
        ],
    )
    assert resp.status_code == 413, resp.text
    assert "per-request" in resp.json()["detail"]


# ── Nothing is left behind ────────────────────────────────────────────────────

def test_rejected_upload_leaves_nothing_on_disk(capped_client, tmp_path):
    pid = _project(capped_client)
    resp = capped_client.post(
        f"/projects/{pid}/files/upload/nifti",
        files=[("files", ("big_T1.nii.gz", io.BytesIO(_blob(_FILE_CAP_MB + 1)), "application/gzip"))],
    )
    assert resp.status_code == 413

    project = tmp_path / "LOCAL_USER" / pid
    # No spool directory survives, and no staging area was created.
    assert not (project / "_upload" / "_spool").exists() or not any(
        (project / "_upload" / "_spool").iterdir()
    )
    assert not (project / "t1").exists()
    # (The project's own creation record is not an upload leftover.)
    leftovers = [p for p in project.rglob("*") if p.is_file() and p != project / "_working" / "project.json"]
    assert leftovers == [], leftovers


# ── Under the cap still works ─────────────────────────────────────────────────

def test_upload_under_the_cap_still_succeeds(capped_client, tmp_path):
    """The caps must not break an ordinary upload: one small NIfTI, staged and committed."""
    pid = _project(capped_client)
    resp = capped_client.post(
        f"/projects/{pid}/files/upload/nifti",
        files=[("files", ("sub001_T1.nii.gz", io.BytesIO(_blob(0.5)), "application/gzip"))],
    )
    assert resp.status_code == 202, resp.text
    staging_id = resp.json()["staging_id"]

    resp = capped_client.post(
        f"/projects/{pid}/files/stage/{staging_id}/commit",
        json={"mappings": [{"filename": "sub001_T1.nii.gz", "mrid": "sub001", "modality": "t1"}]},
    )
    assert resp.status_code == 200, resp.text
    committed = tmp_path / "LOCAL_USER" / pid / "t1" / "sub001.nii.gz"
    assert committed.exists()
    # The test blob isn't gzip, so commit compressed it (stored images are always gzip).
    assert len(gzip.decompress(committed.read_bytes())) == int(0.5 * _MIB)


def test_zip_upload_under_the_cap_still_succeeds(capped_client, tmp_path):
    pid = _project(capped_client)
    zip_bytes = _zip_of("sub001_T1.nii.gz", _blob(0.25))
    resp = capped_client.post(
        f"/projects/{pid}/files/upload/nifti/zip",
        files={"file": ("upload.zip", io.BytesIO(zip_bytes), "application/zip")},
    )
    assert resp.status_code == 202, resp.text
    assert len(resp.json()["proposals"]) == 1


# ── Archive expansion bound ───────────────────────────────────────────────────

def _bomb_zip(name, uncompressed_mib):
    """A small archive whose single member expands past the per-request cap."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, b"\x00" * int(uncompressed_mib * _MIB))
    buf.seek(0)
    return buf.read()


# (endpoint suffix, member name inside the archive)
_ZIP_ENDPOINTS = [
    ("/files/upload/nifti/zip", "sub001_T1.nii.gz"),
    ("/files/upload/bids", "ds/sub-01/anat/sub-01_T1w.nii.gz"),
    ("/files/upload/idat", "sample.idat"),
    ("/files/dicom/upload", "series.dcm"),
]


@pytest.mark.parametrize("suffix,member", _ZIP_ENDPOINTS)
def test_zip_endpoint_rejects_an_archive_that_expands_past_the_cap(
    capped_client, tmp_path, suffix, member
):
    """The upload cap bounds transferred bytes; this bounds extracted bytes.

    The archive is well under the per-file cap — that is the point. Without an
    expansion bound a compliant upload still fills the disk.
    """
    pid = _project(capped_client, "bomb")
    payload = _bomb_zip(member, _REQUEST_CAP_MB + 1)
    assert len(payload) < _FILE_CAP_MB * _MIB  # passes the upload cap

    resp = capped_client.post(
        f"/projects/{pid}{suffix}",
        files={"file": ("upload.zip", io.BytesIO(payload), "application/zip")},
    )
    assert resp.status_code == 413, resp.text
    assert str(_REQUEST_CAP_MB) in resp.json()["detail"]


def test_rejected_archive_leaves_nothing_on_disk(capped_client, tmp_path):
    """Including for IDAT, which extracts straight into the live idat/ folder."""
    pid = _project(capped_client, "bombidat")
    resp = capped_client.post(
        f"/projects/{pid}/files/upload/idat",
        files={"file": (
            "upload.zip",
            io.BytesIO(_bomb_zip("sample.idat", _REQUEST_CAP_MB + 1)),
            "application/zip",
        )},
    )
    assert resp.status_code == 413

    project = tmp_path / "LOCAL_USER" / pid
    # ``_working/ingest_log.json`` records the rejected attempt and is meant to
    # survive; no uploaded content may.
    leftovers = [
        str(p.relative_to(project))
        for p in project.rglob("*")
        if p.is_file() and not p.is_relative_to(project / "_working")
    ]
    assert leftovers == [], leftovers


def test_archive_within_the_expansion_bound_still_extracts(capped_client, tmp_path):
    pid = _project(capped_client, "goodzip")
    resp = capped_client.post(
        f"/projects/{pid}/files/upload/idat",
        files={"file": (
            "upload.zip",
            io.BytesIO(_bomb_zip("sample.idat", 1)),
            "application/zip",
        )},
    )
    assert resp.status_code == 204, resp.text
    idat = tmp_path / "LOCAL_USER" / pid / "idat" / "sample.idat"
    assert idat.stat().st_size == _MIB
