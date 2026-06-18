"""
Tests for GET /catalog/pipelines and GET /catalog/tools.

Uses the dummy_sleep tool and dummy_pipeline pipeline that live in resources/.
These endpoints are public — no auth token needed.
"""

import pytest


def test_list_tools_includes_dummy(local_client):
    resp = local_client.get("/catalog/tools")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert "dummy_sleep" in ids


def test_get_tool_dummy_sleep(local_client):
    resp = local_client.get("/catalog/tools/dummy_sleep")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "dummy_sleep"
    assert data["name"] == "dummy_sleep"
    assert "duration_seconds" in data["parameters"]
    assert data["resources"]["vcpus"] == 1
    assert data["time_per_subject_seconds"] is None


def test_get_tool_not_found(local_client):
    resp = local_client.get("/catalog/tools/nonexistent_tool")
    assert resp.status_code == 404


def test_list_pipelines_includes_dummy(local_client):
    resp = local_client.get("/catalog/pipelines")
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()]
    assert "dummy_pipeline" in ids


def test_get_pipeline_dummy(local_client):
    resp = local_client.get("/catalog/pipelines/dummy_pipeline")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "dummy_pipeline"
    assert data["name"] == "dummy_pipeline"
    assert data["categories"] == ["testing"]
    assert len(data["steps"]) == 1
    assert data["steps"][0]["tool"] == "dummy_sleep"


def test_get_pipeline_not_found(local_client):
    resp = local_client.get("/catalog/pipelines/no_such_pipeline")
    assert resp.status_code == 404


def test_catalog_requires_no_auth(local_client):
    """Catalog endpoints must work without any Authorization header."""
    for url in ["/catalog/pipelines", "/catalog/tools"]:
        resp = local_client.get(url, headers={})
        assert resp.status_code == 200
