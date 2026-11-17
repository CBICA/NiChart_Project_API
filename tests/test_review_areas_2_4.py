"""
Tests for the API side of the review fixes in areas 2 and 4 (2026-11-17):

- DICOM folder uploads never overwrite a file that shares a name (2.2).
- DICOM staging is removed after a successful conversion (2.5).
- Two series can't be converted onto one MRID + modality (2.6).
- Upload-agreement acceptances are recorded server-side (2.19).
- Runs expose ``cancelled`` (4.4).
- Logs can be fetched incrementally (4.8); run listing reuses parsed run files.
- ``GET /jobs/pipelines/finished?since=`` doesn't touch the shared cursor (4.9).
"""

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_dicom import make_dicom_zip


@pytest.fixture(autouse=True)
def _clear_run_store():
    from app.services import job_service
    job_service._data_root = None
    job_service._summary_cache.clear()
    yield
    job_service._data_root = None
    job_service._summary_cache.clear()


def _project(client, name):
    assert client.post("/projects", json={"name": name}).status_code == 201
    return name


def _dcm_bytes(series_uid, patient_id="P01"):
    with zipfile.ZipFile(io.BytesIO(make_dicom_zip(series_uid=series_uid, patient_id=patient_id))) as zf:
        return zf.read("series.dcm")


# ── 2.2 ───────────────────────────────────────────────────────────────────────

def test_dicom_files_with_the_same_name_are_all_kept(data_client, tmp_path):
    pid = _project(data_client, "dcmnames")
    files = [
        ("files", ("1-01.dcm", io.BytesIO(_dcm_bytes("1.2.3.1")), "application/dicom")),
        ("files", ("1-01.dcm", io.BytesIO(_dcm_bytes("1.2.3.2")), "application/dicom")),
    ]
    resp = data_client.post(f"/projects/{pid}/files/dicom/upload/files", files=files)
    assert resp.status_code == 201 or resp.status_code == 202, resp.text
    staging_id = resp.json()["staging_id"]
    series = data_client.get(f"/projects/{pid}/files/dicom/{staging_id}/series").json()["series"]
    assert sorted(s["series_uid"] for s in series) == ["1.2.3.1", "1.2.3.2"]


# ── 2.5 / 2.6 ─────────────────────────────────────────────────────────────────

def _two_series_staging(client, pid):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/x.dcm", _dcm_bytes("1.2.9.1", "SAME"))
        zf.writestr("b/x.dcm", _dcm_bytes("1.2.9.2", "SAME"))
    buf.seek(0)
    resp = client.post(
        f"/projects/{pid}/files/dicom/upload",
        files={"file": ("d.zip", buf, "application/zip")},
    )
    assert resp.status_code in (201, 202), resp.text
    return resp.json()["staging_id"]


def test_convert_rejects_two_series_onto_one_file(job_client):
    pid = _project(job_client, "dcmdup")
    staging_id = _two_series_staging(job_client, pid)
    resp = job_client.post(
        f"/projects/{pid}/files/dicom/{staging_id}/convert",
        json={"series_mappings": [
            {"series_uid": "1.2.9.1", "nichart_modality": "t1"},
            {"series_uid": "1.2.9.2", "nichart_modality": "t1"},
        ]},
    )
    assert resp.status_code == 400
    assert "t1/SAME.nii.gz" in resp.json()["detail"]


def test_dicom_staging_removed_after_successful_conversion(job_client, tmp_path):
    pid = _project(job_client, "dcmclean")
    staging_id = _two_series_staging(job_client, pid)
    staging_dir = tmp_path / "LOCAL_USER" / pid / "_upload" / "dicoms" / staging_id
    assert staging_dir.is_dir()
    resp = job_client.post(
        f"/projects/{pid}/files/dicom/{staging_id}/convert",
        json={"series_mappings": [
            {"series_uid": "1.2.9.1", "nichart_modality": "t1", "mrid": "a01"},
            {"series_uid": "1.2.9.2", "nichart_modality": "t1", "mrid": "a02"},
        ]},
    )
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert job_client.get(f"/jobs/pipelines/{run_id}").json()["status"] == "succeeded"
    assert not staging_dir.exists()


# ── 2.19 ──────────────────────────────────────────────────────────────────────

def test_agreement_acceptance_is_recorded(data_client, tmp_path):
    assert data_client.get("/agreements").json() == {"records": []}
    resp = data_client.post("/agreements", json={"agreement": "upload", "version": "2026-05"})
    assert resp.status_code == 201
    rec = resp.json()
    assert rec["agreement"] == "upload" and rec["version"] == "2026-05" and rec["accepted_at"]
    records = data_client.get("/agreements").json()["records"]
    assert [(r["agreement"], r["version"]) for r in records] == [("upload", "2026-05")]
    stored = (tmp_path / "_working" / "agreements" / "LOCAL_USER.jsonl").read_text().splitlines()
    assert len(stored) == 1 and json.loads(stored[0])["version"] == "2026-05"


@pytest.mark.parametrize("body", [
    {"agreement": "../x", "version": "1"},
    {"agreement": "upload", "version": ""},
    {"agreement": "upload"},
])
def test_agreement_rejects_bad_input(data_client, body):
    assert data_client.post("/agreements", json=body).status_code == 422


# ── 4.4 / 4.8 / 4.9 ───────────────────────────────────────────────────────────

def _submit(client, pid):
    resp = client.post(f"/projects/{pid}/jobs/pipelines", json={"pipeline_id": "test_pipeline"})
    assert resp.status_code in (201, 202), resp.text
    return resp.json()["run_id"]


def test_runs_expose_cancelled(job_client):
    from app.services import job_service
    pid = _project(job_client, "cancelflag")
    run_id = _submit(job_client, pid)
    assert job_client.get(f"/jobs/pipelines/{run_id}").json()["cancelled"] is False
    run = job_service._load_run(run_id)
    run.cancelled = True
    job_service._save_run(run)
    assert job_client.get(f"/jobs/pipelines/{run_id}").json()["cancelled"] is True
    listed = job_client.get(f"/jobs/pipelines?project_id={pid}").json()
    assert listed[0]["cancelled"] is True


def test_run_list_sees_changes_to_cached_runs(job_client):
    from app.services import job_service
    pid = _project(job_client, "listcache")
    run_id = _submit(job_client, pid)
    assert job_client.get(f"/jobs/pipelines?project_id={pid}").json()[0]["status"] == "succeeded"
    run = job_service._load_run(run_id)
    run.status = "failed"
    run.error = "changed on disk"
    job_service._save_run(run)
    assert job_client.get(f"/jobs/pipelines?project_id={pid}").json()[0]["status"] == "failed"


def test_slice_logs():
    from app.services.job_service import slice_logs
    full = "line one\nline two\nline three"
    # Matching tail: only the new text.
    r = slice_logs("r", full, 8, "line one")
    assert (r.offset, r.logs, r.total_length) == (8, "\nline two\nline three", len(full))
    # Tail doesn't match (earlier text changed): the full text.
    r = slice_logs("r", full, 8, "LINE ONE")
    assert (r.offset, r.logs) == (0, full)
    # Offset past the end (text got shorter): the full text.
    r = slice_logs("r", full, 999, "x")
    assert (r.offset, r.logs) == (0, full)
    # No offset: the full text.
    assert slice_logs("r", full, None, None).logs == full


def test_logs_endpoint_accepts_offset(job_client):
    pid = _project(job_client, "logoffset")
    run_id = _submit(job_client, pid)
    full = job_client.get(f"/jobs/pipelines/{run_id}/logs").json()
    assert full["offset"] == 0 and full["total_length"] == len(full["logs"])
    if len(full["logs"]) >= 4:
        n = len(full["logs"])
        part = job_client.get(
            f"/jobs/pipelines/{run_id}/logs", params={"offset": n, "tail": full["logs"][-4:]},
        ).json()
        assert part == {"run_id": run_id, "logs": "", "offset": n, "total_length": n}


def test_finished_since_leaves_the_cursor_alone(job_client):
    pid = _project(job_client, "sincefeed")
    _submit(job_client, pid)
    before = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    after = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = job_client.get("/jobs/pipelines/finished", params={"since": before}).json()
    assert len(r["runs"]) == 1 and r["polled_at"]
    assert job_client.get("/jobs/pipelines/finished", params={"since": after}).json()["runs"] == []
    # The shared cursor was never moved: a cursor-based poll still sees the run.
    assert len(job_client.get("/jobs/pipelines/finished").json()["runs"]) == 1
