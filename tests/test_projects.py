"""
Tests for the project CRUD endpoints (GET/POST/DELETE /projects).
"""

import pytest


def test_list_projects_empty(data_client):
    resp = data_client.get("/projects")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_project(data_client):
    resp = data_client.post("/projects", json={"name": "myproject"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == "myproject"
    assert data["created_at"] is not None


def test_list_projects_after_create(data_client):
    data_client.post("/projects", json={"name": "proj-a"})
    data_client.post("/projects", json={"name": "proj-b"})
    resp = data_client.get("/projects")
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()]
    assert "proj-a" in ids
    assert "proj-b" in ids


def test_create_project_duplicate(data_client):
    data_client.post("/projects", json={"name": "dup"})
    resp = data_client.post("/projects", json={"name": "dup"})
    assert resp.status_code == 409


def test_create_project_invalid_name_special_chars(data_client):
    resp = data_client.post("/projects", json={"name": "bad name!"})
    assert resp.status_code == 422  # Pydantic validation


def test_create_project_invalid_name_leading_hyphen(data_client):
    resp = data_client.post("/projects", json={"name": "-bad"})
    assert resp.status_code == 422


def test_delete_project(data_client):
    data_client.post("/projects", json={"name": "todelete"})
    resp = data_client.delete("/projects/todelete")
    assert resp.status_code == 204
    # Should be gone
    resp = data_client.get("/projects")
    ids = [p["id"] for p in resp.json()]
    assert "todelete" not in ids


def test_delete_project_not_found(data_client):
    resp = data_client.delete("/projects/doesnotexist")
    assert resp.status_code == 404


def test_delete_project_path_traversal(data_client):
    resp = data_client.delete("/projects/../../../etc")
    # FastAPI normalises path params so .. is rejected at routing level (404 or 422)
    assert resp.status_code in (400, 404, 422)
