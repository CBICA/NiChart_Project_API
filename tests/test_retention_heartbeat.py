"""
Tests for project retention: what refreshes the clock, and what happens when it
runs out.

The clock is the LastModified of a single heartbeat object, so a project can be
deleted while in active use unless something explicitly refreshes it — pipeline
submission and uploads both do. Past expiry, the API reaps the project itself
rather than waiting for the sweep Lambda, once it is more than
``project_expiry_grace_hours`` overdue.

The reaping tests drive expiry through the local override file, which
``resolve_expiry`` honours ahead of S3, so they exercise the real endpoints with
no S3 involved at all.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.dependencies import require_auth
from app.services import file_service, retention_service, s3_sync_service

from .conftest import _override_require_auth

_USER = "LOCAL_USER"  # the fixed identity every client fixture authenticates as


@pytest.fixture
def cloud_upload_client(tmp_path, monkeypatch):
    """Cloud-mode client with a data bucket, an isolated data root and no real S3.

    Cloud mode is what makes ``publish_uploads`` and the S3 side of deletion run
    at all. The heartbeat reads and writes are stubbed here, because project
    creation writes one — so expiry is unknown unless a test sets an override —
    the upload sync is a no-op, and each test stubs the s3_sync_service call it
    asserts on. Nothing in here reaches AWS.
    """
    from app.config import Settings, get_settings
    from app.main import create_app

    async def _no_write(**_kw):
        return datetime.now(timezone.utc)

    async def _no_heartbeat(**_kw):
        return None

    async def _no_sync(*_a, **_kw):
        return None

    monkeypatch.setattr(retention_service, "write_heartbeat", _no_write)
    monkeypatch.setattr(retention_service, "read_heartbeat", _no_heartbeat)
    monkeypatch.setattr(s3_sync_service, "sync_to_s3", _no_sync)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        execution_mode="cloud",
        data_root=tmp_path,
        s3_data_bucket="test-bucket",
        s3_data_prefix="fsx",
    )
    app.dependency_overrides[require_auth] = _override_require_auth

    with TestClient(app, raise_server_exceptions=True) as client:
        yield client

    app.dependency_overrides.clear()


def _create_project(client, name="retentionproj"):
    assert client.post("/projects", json={"name": name}).status_code == 201
    return name


# ── touch_project ────────────────────────────────────────────────────────────

def test_touch_project_is_a_noop_without_a_bucket(monkeypatch):
    """Local mode has no S3 at all; nothing must be attempted."""
    calls = []

    async def _spy(**kw):
        calls.append(kw)

    monkeypatch.setattr(retention_service, "write_heartbeat", _spy)
    asyncio.run(retention_service.touch_project(None, "fsx", "u", "p", "test"))
    assert calls == []


def test_touch_project_writes_the_heartbeat(monkeypatch):
    calls = []

    async def _spy(**kw):
        calls.append(kw)

    monkeypatch.setattr(retention_service, "write_heartbeat", _spy)
    asyncio.run(retention_service.touch_project("buck", "fsx", "user-1", "proj", "test"))

    assert calls == [
        {"bucket": "buck", "prefix": "fsx", "user_id": "user-1", "project_id": "proj"}
    ]


def test_touch_project_swallows_failures(monkeypatch):
    """A broken heartbeat must never fail the action that triggered it."""
    async def _boom(**_kw):
        raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(retention_service, "write_heartbeat", _boom)
    asyncio.run(retention_service.touch_project("buck", "fsx", "u", "p", "test"))


# ── wired into submission ────────────────────────────────────────────────────

def test_pipeline_submission_refreshes_retention(job_client, monkeypatch):
    calls = []

    async def _spy(bucket, prefix, user_id, project_id, activity):
        calls.append((project_id, activity))

    monkeypatch.setattr(retention_service, "touch_project", _spy)
    pid = _create_project(job_client, "retentionsubmit")
    resp = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "test_pipeline", "params": {"duration_seconds": 1}},
    )

    assert resp.status_code == 202
    assert calls == [(pid, "pipeline submission")]


def test_rejected_submission_does_not_refresh_retention(job_client, monkeypatch):
    """An invalid request must not keep a project alive."""
    calls = []

    async def _spy(*_a, **_kw):
        calls.append(1)

    monkeypatch.setattr(retention_service, "touch_project", _spy)
    pid = _create_project(job_client, "retentionreject")
    resp = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "no_such_pipeline"},
    )

    assert resp.status_code == 404
    assert calls == []


def test_submission_survives_a_heartbeat_failure(job_client, monkeypatch):
    async def _boom(**_kw):
        raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(retention_service, "write_heartbeat", _boom)
    pid = _create_project(job_client, "retentionbroken")
    resp = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "test_pipeline", "params": {"duration_seconds": 1}},
    )
    assert resp.status_code == 202


# ── refresh on upload ────────────────────────────────────────────────────────

def test_upload_refreshes_retention(cloud_upload_client, monkeypatch):
    """Adding data to a project is active use; the clock is otherwise untouched."""
    calls = []

    async def _spy(bucket, prefix, user_id, project_id, activity):
        calls.append((project_id, activity))

    monkeypatch.setattr(retention_service, "touch_project", _spy)
    pid = _create_project(cloud_upload_client, "retentionupload")
    resp = cloud_upload_client.post(
        f"/projects/{pid}/files/upload/csv",
        files={"file": ("participants.csv", b"MRID,Age\nsub01,70\n", "text/csv")},
    )

    assert resp.status_code == 204, resp.text
    assert calls == [(pid, "upload")]


def test_failed_s3_push_does_not_refresh_retention(cloud_upload_client, monkeypatch):
    """A request that could not save the upload must not keep the project alive."""
    calls = []

    async def _spy(*_a, **_kw):
        calls.append(1)

    async def _boom(*_a, **_kw):
        raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(retention_service, "touch_project", _spy)
    monkeypatch.setattr(s3_sync_service, "sync_to_s3", _boom)
    pid = _create_project(cloud_upload_client, "retentionuploadfail")
    resp = cloud_upload_client.post(
        f"/projects/{pid}/files/upload/csv",
        files={"file": ("participants.csv", b"MRID,Age\nsub01,70\n", "text/csv")},
    )

    assert resp.status_code == 502
    assert calls == []


# ── explicit deletion clears cloud storage ───────────────────────────────────

def test_delete_project_deletes_the_s3_prefix(cloud_upload_client, monkeypatch, tmp_path):
    deleted = []

    async def _spy(bucket, prefix):
        deleted.append((bucket, prefix))

    monkeypatch.setattr(s3_sync_service, "delete_s3_prefix", _spy)
    pid = _create_project(cloud_upload_client, "retentiondelete")
    assert cloud_upload_client.delete(f"/projects/{pid}").status_code == 204

    assert deleted == [("test-bucket", f"fsx/{_USER}/{pid}")]
    assert not (tmp_path / _USER / pid).exists()


def test_delete_project_keeps_everything_when_s3_fails(cloud_upload_client, monkeypatch, tmp_path):
    """Nothing is destroyed on a failed cloud delete, so the request can be retried."""
    async def _boom(*_a, **_kw):
        raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(s3_sync_service, "delete_s3_prefix", _boom)
    pid = _create_project(cloud_upload_client, "retentiondeletefail")
    resp = cloud_upload_client.delete(f"/projects/{pid}")

    assert resp.status_code == 502
    assert (tmp_path / _USER / pid).is_dir()


def test_delete_project_without_a_bucket_touches_no_s3(data_client, monkeypatch):
    async def _spy(*_a, **_kw):
        raise AssertionError("local mode must not call S3")

    monkeypatch.setattr(s3_sync_service, "delete_s3_prefix", _spy)
    pid = _create_project(data_client, "retentiondeletelocal")
    assert data_client.delete(f"/projects/{pid}").status_code == 204


# ── resolve_expiry ───────────────────────────────────────────────────────────

def test_resolve_expiry_is_none_without_a_bucket(tmp_path):
    expiry = asyncio.run(
        retention_service.resolve_expiry(None, "fsx", "u", "p", tmp_path, 7)
    )
    assert expiry is None


def test_resolve_expiry_is_none_when_the_heartbeat_is_absent(tmp_path, monkeypatch):
    """A project that was never given a clock must not be treated as expired."""
    async def _absent(**_kw):
        return None

    monkeypatch.setattr(retention_service, "read_heartbeat", _absent)
    expiry = asyncio.run(
        retention_service.resolve_expiry("buck", "fsx", "u", "p", tmp_path, 7)
    )
    assert expiry is None


def test_resolve_expiry_fails_open_on_an_s3_error(tmp_path, monkeypatch):
    """A network blip must not make a project look expired."""
    async def _boom(**_kw):
        raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(retention_service, "read_heartbeat", _boom)
    expiry = asyncio.run(
        retention_service.resolve_expiry("buck", "fsx", "u", "p", tmp_path, 7)
    )
    assert expiry is None


def test_resolve_expiry_adds_the_window_to_the_heartbeat(tmp_path, monkeypatch):
    written = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def _hb(**_kw):
        return written

    monkeypatch.setattr(retention_service, "read_heartbeat", _hb)
    expiry = asyncio.run(
        retention_service.resolve_expiry("buck", "fsx", "u", "p", tmp_path, 7)
    )
    assert expiry == written + timedelta(days=7)


def test_resolve_expiry_prefers_a_local_override(tmp_path, monkeypatch):
    async def _hb(**_kw):
        raise AssertionError("the override must short-circuit the S3 read")

    monkeypatch.setattr(retention_service, "read_heartbeat", _hb)
    override = datetime(2030, 6, 1, tzinfo=timezone.utc)
    retention_service.write_retention_override(tmp_path, override)
    expiry = asyncio.run(
        retention_service.resolve_expiry("buck", "fsx", "u", "p", tmp_path, 7)
    )
    assert expiry == override


# ── overdue_by ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "expiry,expected",
    [
        (None, None),
        (datetime(2030, 1, 1, tzinfo=timezone.utc), None),   # not yet due
    ],
)
def test_overdue_by_returns_none_when_not_overdue(expiry, expected):
    assert retention_service.overdue_by(expiry) == expected


def test_overdue_by_measures_from_the_expiry():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    overdue = retention_service.overdue_by(datetime(2026, 1, 8, tzinfo=timezone.utc), now=now)
    assert overdue == timedelta(days=2)


# ── lazy reaping of long-expired projects ────────────────────────────────────
#
# Driven through the local override, which resolve_expiry honours ahead of S3,
# so these exercise the real endpoints with no S3 at all.

def _expire(client, tmp_path, name, *, hours_ago):
    pid = _create_project(client, name)
    pdir = tmp_path / _USER / pid
    retention_service.write_retention_override(
        pdir, datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    )
    return pid, pdir


def test_project_expired_past_the_grace_window_is_deleted_on_listing(data_client, tmp_path):
    pid, pdir = _expire(data_client, tmp_path, "reapme", hours_ago=25)

    resp = data_client.get("/projects")

    assert resp.status_code == 200
    assert pid not in [p["id"] for p in resp.json()]
    assert not pdir.exists()


def test_project_inside_the_grace_window_is_still_listed(data_client, tmp_path):
    pid, pdir = _expire(data_client, tmp_path, "notyetreaped", hours_ago=23)

    resp = data_client.get("/projects")

    assert pid in [p["id"] for p in resp.json()]
    assert pdir.is_dir()


def test_live_projects_are_untouched_by_a_listing(data_client, tmp_path):
    pid = _create_project(data_client, "liveproject")
    assert pid in [p["id"] for p in data_client.get("/projects").json()]
    assert (tmp_path / _USER / pid).is_dir()


def test_a_project_without_a_clock_is_never_reaped(data_client, tmp_path):
    """No heartbeat, no bucket, no override — expiry is unknown, so hands off."""
    pid = _create_project(data_client, "noclockproject")
    assert data_client.get("/projects").status_code == 200
    assert (tmp_path / _USER / pid).is_dir()


def test_retention_query_on_a_long_expired_project_reaps_and_404s(data_client, tmp_path):
    pid, pdir = _expire(data_client, tmp_path, "reaponquery", hours_ago=48)

    resp = data_client.get(f"/projects/{pid}/retention")

    assert resp.status_code == 404
    assert not pdir.exists()


def test_retention_query_inside_the_grace_window_reports_a_past_expiry(data_client, tmp_path):
    pid, pdir = _expire(data_client, tmp_path, "warnbutkeep", hours_ago=2)

    resp = data_client.get(f"/projects/{pid}/retention")

    assert resp.status_code == 200
    assert datetime.fromisoformat(resp.json()["expires_at"]) < datetime.now(timezone.utc)
    assert pdir.is_dir()


def test_refresh_cannot_revive_a_long_expired_project(data_client, tmp_path):
    pid, pdir = _expire(data_client, tmp_path, "cannotrevive", hours_ago=25)

    resp = data_client.post(f"/projects/{pid}/retention/refresh")

    assert resp.status_code == 404
    assert not pdir.exists()


def test_refresh_inside_the_grace_window_clears_the_override(data_client, tmp_path):
    """The override is a test fixture, not a verdict: a real refresh drops it."""
    pid, pdir = _expire(data_client, tmp_path, "refreshinwindow", hours_ago=2)

    # Local mode has no bucket, so the refresh itself 404s — but only after the
    # override has been cleared, which is what returns the project to normal.
    data_client.post(f"/projects/{pid}/retention/refresh")

    assert pdir.is_dir()
    assert retention_service.read_retention_override(pdir) is None
    assert pid in [p["id"] for p in data_client.get("/projects").json()]


def test_reaping_a_project_also_clears_its_s3_prefix(cloud_upload_client, monkeypatch, tmp_path):
    deleted = []

    async def _spy(bucket, prefix):
        deleted.append((bucket, prefix))

    monkeypatch.setattr(s3_sync_service, "delete_s3_prefix", _spy)
    pid, pdir = _expire(cloud_upload_client, tmp_path, "reapwiths3", hours_ago=30)

    assert cloud_upload_client.get("/projects").status_code == 200

    assert not pdir.exists()
    assert deleted == [("test-bucket", f"fsx/{_USER}/{pid}")]


def test_reaping_survives_a_failed_s3_delete(cloud_upload_client, monkeypatch, tmp_path):
    """The local copy still goes, so the project stops existing as far as the API is concerned."""
    async def _boom(*_a, **_kw):
        raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(s3_sync_service, "delete_s3_prefix", _boom)
    pid, pdir = _expire(cloud_upload_client, tmp_path, "reaps3fails", hours_ago=30)

    resp = cloud_upload_client.get("/projects")

    assert resp.status_code == 200
    assert pid not in [p["id"] for p in resp.json()]
    assert not pdir.exists()


def test_a_failed_local_delete_leaves_the_project_alone(
    cloud_upload_client, monkeypatch, tmp_path
):
    """If the tree cannot be removed the project is untouched, not half-deleted.

    It stays listed and keeps its cloud copy and its heartbeat, so the expiry is
    still readable and the next query simply tries again.
    """
    deleted = []

    async def _spy(bucket, prefix):
        deleted.append(prefix)

    def _boom(_path):
        raise OSError("device busy")

    monkeypatch.setattr(s3_sync_service, "delete_s3_prefix", _spy)
    pid, pdir = _expire(cloud_upload_client, tmp_path, "reaplocalfails", hours_ago=30)
    monkeypatch.setattr(file_service.shutil, "rmtree", _boom)

    resp = cloud_upload_client.get("/projects")

    assert resp.status_code == 200
    assert pid in [p["id"] for p in resp.json()]
    assert pdir.is_dir()
    assert deleted == []  # the clock survives, so the next listing tries again


def test_expire_soon_accepts_a_past_expiry(data_client, tmp_path):
    """The test endpoint is how the frontend exercises the deletion path."""
    resp = data_client.post(
        "/test/projects/expire-soon",
        json={"name": "pastexpiry", "expires_in_minutes": -1500},
    )
    assert resp.status_code == 201
    assert data_client.get("/projects/pastexpiry/retention").status_code == 404
    assert not (tmp_path / _USER / "pastexpiry").exists()


def test_nifti_staging_refreshes_retention(cloud_upload_client, monkeypatch):
    """Staged files are not synced to S3, so this path refreshes the clock itself."""
    calls = []

    async def _spy(bucket, prefix, user_id, project_id, activity):
        calls.append((project_id, activity))

    monkeypatch.setattr(retention_service, "touch_project", _spy)
    pid = _create_project(cloud_upload_client, "retentionstage")
    resp = cloud_upload_client.post(
        f"/projects/{pid}/files/upload/nifti",
        files=[("files", ("sub01_T1.nii.gz", b"not-a-real-nifti", "application/gzip"))],
    )

    assert resp.status_code == 202, resp.text
    assert calls == [(pid, "upload")]
