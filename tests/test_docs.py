"""
Tests for GET /catalog/docs, /catalog/docs/{docs_id},
and GET /catalog/docs/{docs_id}/{path}.

All docs endpoints are public — no auth token needed.
"""

import pytest


# ── Index ─────────────────────────────────────────────────────────────────────

def test_list_docs_returns_list(local_client):
    resp = local_client.get("/catalog/docs")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_list_docs_includes_dlmuse(local_client):
    resp = local_client.get("/catalog/docs")
    assert resp.status_code == 200
    ids = [t["docs_id"] for t in resp.json()]
    assert "dlmuse" in ids


def test_list_docs_topic_has_expected_fields(local_client):
    resp = local_client.get("/catalog/docs")
    assert resp.status_code == 200
    dlmuse = next(t for t in resp.json() if t["docs_id"] == "dlmuse")
    assert "title" in dlmuse
    assert "pipelines" in dlmuse
    assert "run_dlmuse" in dlmuse["pipelines"]
    assert "run_dlmuse_harmonized" in dlmuse["pipelines"]


def test_list_docs_includes_spare(local_client):
    resp = local_client.get("/catalog/docs")
    assert resp.status_code == 200
    ids = [t["docs_id"] for t in resp.json()]
    assert "spare" in ids


def test_list_docs_spare_covers_all_variants(local_client):
    resp = local_client.get("/catalog/docs")
    spare = next(t for t in resp.json() if t["docs_id"] == "spare")
    for pid in ["run_spare_all", "run_spare_all_harmonized",
                "run_spare_all_cvm", "run_spare_all_cvm_harmonized"]:
        assert pid in spare["pipelines"]


# ── Manifest ──────────────────────────────────────────────────────────────────

def test_get_manifest_dlmuse(local_client):
    resp = local_client.get("/catalog/docs/dlmuse")
    assert resp.status_code == 200
    data = resp.json()
    assert data["docs_id"] == "dlmuse"
    assert data["title"]
    assert len(data["sections"]) >= 1


def test_get_manifest_sections_have_required_fields(local_client):
    resp = local_client.get("/catalog/docs/dlmuse")
    assert resp.status_code == 200
    for section in resp.json()["sections"]:
        assert "id" in section
        assert "title" in section
        assert "file" in section
        assert section["audience"] in ("user", "developer", "all")
        assert section["type"] in ("markdown", "data", "image")


def test_get_manifest_not_found(local_client):
    resp = local_client.get("/catalog/docs/nonexistent_topic")
    assert resp.status_code == 404


def test_get_manifest_invalid_id(local_client):
    resp = local_client.get("/catalog/docs/../etc")
    # FastAPI path routing makes this a 404 (no route match) or 400
    assert resp.status_code in (400, 404, 422)


# ── File serving ──────────────────────────────────────────────────────────────

def test_get_docs_file_markdown(local_client):
    resp = local_client.get("/catalog/docs/dlmuse/overview.md")
    assert resp.status_code == 200
    assert "DLMUSE" in resp.text


def test_get_docs_file_not_found(local_client):
    resp = local_client.get("/catalog/docs/dlmuse/nonexistent_file.md")
    assert resp.status_code == 404


def test_get_docs_file_topic_not_found(local_client):
    resp = local_client.get("/catalog/docs/nonexistent_topic/overview.md")
    assert resp.status_code == 404


def test_get_docs_file_path_traversal_rejected(local_client):
    resp = local_client.get("/catalog/docs/dlmuse/../../pipelines/run_dlmuse.yaml")
    assert resp.status_code in (400, 404)


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_docs_endpoints_require_no_auth(local_client):
    """All docs endpoints must be accessible without an Authorization header."""
    for url in [
        "/catalog/docs",
        "/catalog/docs/dlmuse",
        "/catalog/docs/dlmuse/overview.md",
    ]:
        resp = local_client.get(url, headers={})
        assert resp.status_code != 401, f"{url} unexpectedly requires auth"


# ── Pipeline catalog integration ──────────────────────────────────────────────

def test_pipeline_catalog_includes_docs_id(local_client):
    resp = local_client.get("/catalog/pipelines/run_dlmuse")
    assert resp.status_code == 200
    assert resp.json()["docs_id"] == "dlmuse"


def test_pipeline_list_includes_docs_id(local_client):
    resp = local_client.get("/catalog/pipelines")
    assert resp.status_code == 200
    dlmuse = next(p for p in resp.json() if p["id"] == "run_dlmuse")
    assert dlmuse["docs_id"] == "dlmuse"


def test_pipeline_without_docs_id_returns_null(local_client):
    resp = local_client.get("/catalog/pipelines/dummy_pipeline")
    assert resp.status_code == 200
    assert resp.json()["docs_id"] is None
