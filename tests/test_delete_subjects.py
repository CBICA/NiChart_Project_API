"""
Tests for removing subjects (POST /projects/{id}/files/subjects/delete): their images in
every modality directory and their participants.csv rows, as one recorded deletion — and
for deletions reaching the S3 copies in cloud mode, which a run would otherwise restore.
"""

import csv
from pathlib import Path

import pytest

from app.services import ingest_log


def _create_project(client, name="proj1"):
    resp = client.post("/projects", json={"name": name})
    assert resp.status_code == 201
    return name


def _seed(pdir: Path) -> None:
    """Three subjects with T1s, one also with a FLAIR (as .nii), a pipeline output, and a
    participants.csv with an extra column and a blank Age."""
    (pdir / "t1").mkdir()
    for m in ("sub01", "sub02", "sub03"):
        (pdir / "t1" / f"{m}.nii.gz").write_bytes(b"t1-" + m.encode())
    # Shares a prefix with sub01 but is a different subject.
    (pdir / "t1" / "sub01_b.nii.gz").write_bytes(b"other")
    (pdir / "fl").mkdir()
    (pdir / "fl" / "sub02.nii").write_bytes(b"fl")
    (pdir / "dlmuse_seg").mkdir()
    (pdir / "dlmuse_seg" / "sub02_DLMUSE.nii.gz").write_bytes(b"seg")
    (pdir / "participants").mkdir()
    (pdir / "participants" / "participants.csv").write_text(
        "MRID,Age,Sex,Note\n"
        "sub01,70,F,keep\n"
        "sub02,,M,\"has, comma\"\n"
        "sub03,,F,x\n"
        "sub01_b,55,M,y\n"
    )


def _rows(pdir: Path) -> list[dict]:
    with (pdir / "participants" / "participants.csv").open(newline="") as f:
        return list(csv.DictReader(f))


def _delete(client, pid, **body):
    return client.post(f"/projects/{pid}/files/subjects/delete", json=body)


def test_dry_run_reports_without_deleting(data_client, tmp_path):
    pid = _create_project(data_client)
    pdir = tmp_path / "LOCAL_USER" / pid
    _seed(pdir)

    resp = _delete(data_client, pid, mrids=["sub02", "sub03", "ghost"], dry_run=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert sorted(body["files"]) == ["fl/sub02.nii", "t1/sub02.nii.gz", "t1/sub03.nii.gz"]
    assert body["removed_rows"] == 2
    assert body["mrids_without_images"] == ["ghost"]
    # Nothing changed.
    assert (pdir / "t1" / "sub02.nii.gz").exists()
    assert (pdir / "fl" / "sub02.nii").exists()
    assert len(_rows(pdir)) == 4


def test_deletes_images_in_every_modality_and_their_rows(data_client, tmp_path):
    pid = _create_project(data_client)
    pdir = tmp_path / "LOCAL_USER" / pid
    _seed(pdir)

    resp = _delete(data_client, pid, mrids=["sub02", "sub03"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed_rows"] == 2

    assert not (pdir / "t1" / "sub02.nii.gz").exists()
    assert not (pdir / "t1" / "sub03.nii.gz").exists()
    assert not (pdir / "fl" / "sub02.nii").exists()
    # Other subjects — including one whose MRID merely starts with a deleted one's — stay.
    assert (pdir / "t1" / "sub01.nii.gz").exists()
    assert (pdir / "t1" / "sub01_b.nii.gz").exists()
    # Pipeline outputs are not touched.
    assert (pdir / "dlmuse_seg" / "sub02_DLMUSE.nii.gz").exists()

    rows = _rows(pdir)
    assert [r["MRID"] for r in rows] == ["sub01", "sub01_b"]
    # Header and the remaining rows' values are preserved, extra columns included.
    assert list(rows[0].keys()) == ["MRID", "Age", "Sex", "Note"]
    assert rows[0] == {"MRID": "sub01", "Age": "70", "Sex": "F", "Note": "keep"}


def test_remove_rows_false_keeps_participants(data_client, tmp_path):
    pid = _create_project(data_client)
    pdir = tmp_path / "LOCAL_USER" / pid
    _seed(pdir)

    resp = _delete(data_client, pid, mrids=["sub03"], remove_rows=False)
    assert resp.status_code == 200
    assert resp.json()["removed_rows"] == 0
    assert not (pdir / "t1" / "sub03.nii.gz").exists()
    assert len(_rows(pdir)) == 4


def test_removing_every_row_keeps_the_header(data_client, tmp_path):
    pid = _create_project(data_client)
    pdir = tmp_path / "LOCAL_USER" / pid
    _seed(pdir)

    resp = _delete(data_client, pid, mrids=["sub01", "sub02", "sub03", "sub01_b"])
    assert resp.status_code == 200
    text = (pdir / "participants" / "participants.csv").read_text()
    assert text.strip() == "MRID,Age,Sex,Note"


def test_mrids_are_matched_not_used_as_paths(data_client, tmp_path):
    pid = _create_project(data_client)
    pdir = tmp_path / "LOCAL_USER" / pid
    _seed(pdir)
    outside = tmp_path / "LOCAL_USER" / "secret.nii.gz"
    outside.write_bytes(b"x")

    resp = _delete(data_client, pid, mrids=["../secret", "../../LOCAL_USER/secret", "*"])
    assert resp.status_code == 200
    assert resp.json()["files"] == []
    assert outside.exists()
    assert len(list((pdir / "t1").iterdir())) == 4


def test_empty_mrid_list_rejected(data_client):
    pid = _create_project(data_client)
    assert _delete(data_client, pid, mrids=[]).status_code == 422


def test_deletion_is_one_provenance_event(data_client, tmp_path):
    pid = _create_project(data_client)
    pdir = tmp_path / "LOCAL_USER" / pid
    _seed(pdir)
    ingest_log.begin(pdir)  # baseline with everything present

    _delete(data_client, pid, mrids=["sub02"])
    events = [e for e in ingest_log.load(pdir)["events"] if e["kind"] == "subjects_delete"]
    assert len(events) == 1
    assert sorted(events[0]["removed"]) == ["fl/sub02.nii", "t1/sub02.nii.gz"]
    assert events[0]["changed"] == ["participants/participants.csv"]


# ── Cloud mode ────────────────────────────────────────────────────────────────

@pytest.fixture
def cloud_client(tmp_path, monkeypatch):
    """Cloud-mode client with S3 calls recorded instead of performed."""
    from fastapi.testclient import TestClient

    from app.auth.dependencies import require_auth
    from app.config import Settings, get_settings
    from app.main import create_app
    from app.services import retention_service, s3_sync_service
    from tests.conftest import _override_require_auth

    calls: list[tuple] = []

    async def _fake_to_s3(local_dir, bucket, prefix):
        calls.append(("up", bucket, prefix))

    async def _fake_delete_files(bucket, prefix, rel_paths):
        calls.append(("delete", bucket, prefix, sorted(rel_paths)))

    async def _fake_touch(*args, **kwargs):
        return None

    monkeypatch.setattr(s3_sync_service, "sync_to_s3", _fake_to_s3)
    monkeypatch.setattr(s3_sync_service, "delete_s3_files", _fake_delete_files)
    monkeypatch.setattr(retention_service, "touch_project", _fake_touch)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        execution_mode="cloud", data_root=tmp_path,
        s3_data_bucket="test-bucket", s3_data_prefix="fsx",
    )
    app.dependency_overrides[require_auth] = _override_require_auth
    with TestClient(app, raise_server_exceptions=True) as client:
        client.calls = calls
        yield client
    app.dependency_overrides.clear()


def test_cloud_deletes_s3_copies_and_pushes_participants(cloud_client, tmp_path):
    pid = _create_project(cloud_client)
    _seed(tmp_path / "LOCAL_USER" / pid)
    cloud_client.calls.clear()

    resp = _delete(cloud_client, pid, mrids=["sub02"])
    assert resp.status_code == 200, resp.text
    # The images' S3 copies go, or the next run's sync down would restore them...
    assert ("delete", "test-bucket", "fsx/LOCAL_USER/proj1",
            ["fl/sub02.nii", "t1/sub02.nii.gz"]) in cloud_client.calls
    # ...and the rewritten participants.csv goes up.
    assert ("up", "test-bucket", "fsx/LOCAL_USER/proj1") in cloud_client.calls


def test_cloud_dry_run_touches_nothing(cloud_client, tmp_path):
    pid = _create_project(cloud_client)
    _seed(tmp_path / "LOCAL_USER" / pid)
    cloud_client.calls.clear()

    assert _delete(cloud_client, pid, mrids=["sub02"], dry_run=True).status_code == 200
    assert cloud_client.calls == []


def test_delete_keys_batches_at_1000(monkeypatch):
    from app.services import s3_sync_service

    batches: list[int] = []

    class _Fake:
        def delete_objects(self, Bucket, Delete):
            batches.append(len(Delete["Objects"]))

    monkeypatch.setattr(s3_sync_service.boto3, "client", lambda svc, **kw: _Fake())
    n = s3_sync_service._delete_keys_sync("b", [f"k{i}" for i in range(2500)])
    assert n == 2500
    assert batches == [1000, 1000, 500]


# ── Single file/folder delete (DELETE /files) in cloud mode ──────────────────

@pytest.fixture
def cloud_delete_client(cloud_client, monkeypatch):
    from app.services import s3_sync_service

    async def _fake_delete_prefix(bucket, prefix):
        cloud_client.calls.append(("delete_prefix", bucket, prefix))

    monkeypatch.setattr(s3_sync_service, "delete_s3_prefix", _fake_delete_prefix)
    return cloud_client


def test_cloud_file_delete_removes_the_s3_copy(cloud_delete_client, tmp_path):
    pid = _create_project(cloud_delete_client)
    _seed(tmp_path / "LOCAL_USER" / pid)
    cloud_delete_client.calls.clear()

    resp = cloud_delete_client.delete(f"/projects/{pid}/files", params={"path": "t1/sub02.nii.gz"})
    assert resp.status_code == 204
    assert cloud_delete_client.calls == [
        ("delete", "test-bucket", "fsx/LOCAL_USER/proj1", ["t1/sub02.nii.gz"])
    ]


def test_cloud_folder_delete_removes_everything_under_it(cloud_delete_client, tmp_path):
    pid = _create_project(cloud_delete_client)
    _seed(tmp_path / "LOCAL_USER" / pid)
    cloud_delete_client.calls.clear()

    resp = cloud_delete_client.delete(f"/projects/{pid}/files", params={"path": "t1/"})
    assert resp.status_code == 204
    # Trailing slash: t1ce/ (sharing the "t1" prefix) must not be matched.
    assert cloud_delete_client.calls == [("delete_prefix", "test-bucket", "fsx/LOCAL_USER/proj1/t1/")]


@pytest.mark.parametrize("path", ["", ".", "t1/.."])
def test_deleting_the_project_folder_itself_is_refused(data_client, tmp_path, path):
    pid = _create_project(data_client)
    pdir = tmp_path / "LOCAL_USER" / pid
    _seed(pdir)
    resp = data_client.delete(f"/projects/{pid}/files", params={"path": path})
    assert resp.status_code == 400
    assert (pdir / "t1" / "sub01.nii.gz").exists()
