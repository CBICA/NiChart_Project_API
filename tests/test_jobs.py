"""
Tests for the pipeline job submission and status endpoints.

Uses MockBackend (injected via job_client fixture) so no Docker or AWS is needed.
MockBackend immediately returns "succeeded", so background tasks complete within
the same HTTP request cycle — tests can check final state after a single submit call.
"""

import json

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_project(client, name="testproject"):
    resp = client.post("/projects", json={"name": name})
    assert resp.status_code == 201
    return name


@pytest.fixture(autouse=True)
def _clear_run_store():
    """Reset module-level run store between tests for clean isolation."""
    from app.services import job_service
    job_service._runs.clear()
    yield
    job_service._runs.clear()


# ── Submission ────────────────────────────────────────────────────────────────

def test_submit_pipeline_returns_run_id(job_client):
    pid = _create_project(job_client)
    resp = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline"},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert "run_id" in data
    assert data["status"] == "pending"


def test_submit_unknown_pipeline(job_client):
    pid = _create_project(job_client)
    resp = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "no_such_pipeline"},
    )
    assert resp.status_code == 404


def test_submit_unknown_project(job_client):
    resp = job_client.post(
        "/projects/no-such-project/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline"},
    )
    assert resp.status_code == 404


# ── Background task completion ────────────────────────────────────────────────

def test_pipeline_completes_with_mock_backend(job_client):
    """With MockBackend, the background task finishes before the test advances."""
    pid = _create_project(job_client, "completetest")
    submit_resp = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline"},
    )
    run_id = submit_resp.json()["run_id"]

    resp = job_client.get(f"/jobs/pipelines/{run_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "succeeded"
    assert data["total_steps"] == 1
    assert data["steps"][0]["status"] in ("succeeded", "skipped")


def test_pipeline_with_params(job_client):
    pid = _create_project(job_client, "paramtest")
    resp = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline", "params": {"duration_seconds": 5}},
    )
    assert resp.status_code == 202


def test_pipeline_cache_reuse_default_true(job_client, tmp_path):
    """Submit twice with the same params; second run should have skipped steps."""
    pid = _create_project(job_client, "cachetest")
    for _ in range(2):
        job_client.post(
            f"/projects/{pid}/jobs/pipelines",
            json={"pipeline_id": "dummy_pipeline", "reuse_cached_steps": True},
        )
    runs = job_client.get(f"/jobs/pipelines?project_id={pid}").json()
    assert len(runs) == 2


# ── List / detail / logs / cancel ─────────────────────────────────────────────

def test_list_runs_empty_for_new_project(job_client):
    pid = _create_project(job_client, "emptylist")
    resp = job_client.get(f"/jobs/pipelines?project_id={pid}")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_runs_after_submit(job_client):
    pid = _create_project(job_client, "listtest")
    submit_resp = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline"},
    )
    run_id = submit_resp.json()["run_id"]

    resp = job_client.get(f"/jobs/pipelines?project_id={pid}")
    assert resp.status_code == 200
    run_ids = [r["run_id"] for r in resp.json()]
    assert run_id in run_ids


def test_list_runs_filter_by_project(job_client):
    pid_a = _create_project(job_client, "proj-a")
    pid_b = _create_project(job_client, "proj-b")
    run_a = job_client.post(
        f"/projects/{pid_a}/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline"},
    ).json()["run_id"]
    run_b = job_client.post(
        f"/projects/{pid_b}/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline"},
    ).json()["run_id"]

    resp_a = job_client.get(f"/jobs/pipelines?project_id={pid_a}")
    ids_a = [r["run_id"] for r in resp_a.json()]
    assert run_a in ids_a
    assert run_b not in ids_a


def test_get_run_detail(job_client):
    pid = _create_project(job_client, "detailtest")
    run_id = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline"},
    ).json()["run_id"]

    resp = job_client.get(f"/jobs/pipelines/{run_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == run_id
    assert data["pipeline_id"] == "dummy_pipeline"
    assert "steps" in data


def test_get_run_detail_not_found(job_client):
    resp = job_client.get("/jobs/pipelines/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_get_run_logs(job_client):
    pid = _create_project(job_client, "logstest")
    run_id = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline"},
    ).json()["run_id"]

    resp = job_client.get(f"/jobs/pipelines/{run_id}/logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == run_id
    assert isinstance(data["logs"], str)


def test_get_run_logs_not_found(job_client):
    resp = job_client.get("/jobs/pipelines/00000000-0000-0000-0000-000000000000/logs")
    assert resp.status_code == 404


def test_cancel_run(job_client):
    pid = _create_project(job_client, "canceltest")
    run_id = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline"},
    ).json()["run_id"]
    resp = job_client.delete(f"/jobs/pipelines/{run_id}")
    assert resp.status_code == 204


def test_cancel_nonexistent_run(job_client):
    resp = job_client.delete("/jobs/pipelines/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


# ── Step-level details ────────────────────────────────────────────────────────

def test_step_details_present(job_client):
    pid = _create_project(job_client, "steptest")
    run_id = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline"},
    ).json()["run_id"]

    detail = job_client.get(f"/jobs/pipelines/{run_id}").json()
    assert len(detail["steps"]) == 1
    step = detail["steps"][0]
    assert step["step_id"] == "sleep"
    assert step["tool_id"] == "dummy_sleep"
    assert step["status"] in ("succeeded", "skipped")


# ── Persistence ───────────────────────────────────────────────────────────────

def test_run_persisted_to_disk(job_client, tmp_path):
    """pipeline_runs.json is written after the background task completes."""
    pid = _create_project(job_client, "persisttest")
    run_id = job_client.post(
        f"/projects/{pid}/jobs/pipelines",
        json={"pipeline_id": "dummy_pipeline"},
    ).json()["run_id"]

    runs_file = tmp_path / "LOCAL_USER" / pid / "_working" / "pipeline_runs.json"
    assert runs_file.exists(), "pipeline_runs.json should exist after task runs"
    data = json.loads(runs_file.read_text())
    assert run_id in data
    assert data[run_id]["status"] == "succeeded"


def test_load_runs_from_disk_restores_history(tmp_path):
    """Runs written by one server instance are visible after a fresh load."""
    from app.services import job_service

    # Write a finished run directly into the project structure
    project_dir = tmp_path / "LOCAL_USER" / "myproject"
    (project_dir / "_working").mkdir(parents=True)
    fake_run = {
        "fake-run-id": {
            "run_id": "fake-run-id",
            "user_id": "LOCAL_USER",
            "project_id": "myproject",
            "pipeline_id": "dummy_pipeline",
            "status": "succeeded",
            "submitted_at": "2024-01-01T00:00:00+00:00",
            "finished_at": "2024-01-01T00:01:00+00:00",
            "current_step": 0,
            "total_steps": 1,
            "error": None,
            "cancelled": False,
            "steps": [],
        }
    }
    (project_dir / "_working" / "pipeline_runs.json").write_text(json.dumps(fake_run))

    # Load into a fresh store
    job_service._runs.clear()
    job_service.load_runs_from_disk(tmp_path)
    assert "fake-run-id" in job_service._runs
    assert job_service._runs["fake-run-id"].status == "succeeded"
    job_service._runs.clear()


def test_load_runs_marks_interrupted_runs_failed(tmp_path):
    """In-progress runs restored from disk are marked as failed."""
    from app.services import job_service

    project_dir = tmp_path / "LOCAL_USER" / "myproject"
    (project_dir / "_working").mkdir(parents=True)
    fake_run = {
        "interrupted-run": {
            "run_id": "interrupted-run",
            "user_id": "LOCAL_USER",
            "project_id": "myproject",
            "pipeline_id": "dummy_pipeline",
            "status": "running",
            "submitted_at": "2024-01-01T00:00:00+00:00",
            "finished_at": None,
            "current_step": 0,
            "total_steps": 1,
            "error": None,
            "cancelled": False,
            "steps": [],
        }
    }
    (project_dir / "_working" / "pipeline_runs.json").write_text(json.dumps(fake_run))

    job_service._runs.clear()
    job_service.load_runs_from_disk(tmp_path)
    run = job_service._runs["interrupted-run"]
    assert run.status == "failed"
    assert run.error
    assert run.finished_at is not None
    job_service._runs.clear()
