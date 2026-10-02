"""
Tests for GET /catalog/pipelines and GET /catalog/tools.

Uses the test_sleep tool and test_pipeline pipeline that live in resources/.
These endpoints are public — no auth token needed.
"""

import pytest


def test_list_tools_includes_dummy(local_client):
    resp = local_client.get("/catalog/tools")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert "test_sleep" in ids


def test_get_tool_test_sleep(local_client):
    resp = local_client.get("/catalog/tools/test_sleep")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "test_sleep"
    assert data["name"] == "test_sleep"
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
    assert "test_pipeline" in ids


def test_get_pipeline_dummy(local_client):
    resp = local_client.get("/catalog/pipelines/test_pipeline")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "test_pipeline"
    assert data["name"] == "test_pipeline"
    assert data["categories"] == ["testing"]
    assert len(data["steps"]) == 1
    assert data["steps"][0]["tool"] == "test_sleep"


def test_get_pipeline_not_found(local_client):
    resp = local_client.get("/catalog/pipelines/no_such_pipeline")
    assert resp.status_code == 404


def test_catalog_requires_no_auth(local_client):
    """Catalog endpoints must work without any Authorization header."""
    for url in ["/catalog/pipelines", "/catalog/tools"]:
        resp = local_client.get(url, headers={})
        assert resp.status_code == 200


def test_get_var_group_catalog(local_client):
    resp = local_client.get("/catalog/var-groups")
    assert resp.status_code == 200
    data = resp.json()

    dlmuse_group = data["groups"]["group_dlmuse-vol"]
    assert dlmuse_group["label"] == "DLMUSE Volumes"
    assert dlmuse_group["prefix"] == "DL_MUSE_Volume_"
    assert dlmuse_group["values"] is None

    spare_group = data["groups"]["group_spare-scores"]
    assert spare_group["prefix"] is None
    assert "SPARE_AD" in spare_group["values"]

    roi_list = data["roi_lists"]["list_muse-single"]
    assert roi_list["atlas"] == "muse"
    assert 47 in roi_list["values"]


def test_var_group_catalog_requires_no_auth(local_client):
    resp = local_client.get("/catalog/var-groups", headers={})
    assert resp.status_code == 200
