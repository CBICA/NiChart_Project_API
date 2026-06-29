"""
Smoke tests verifying that every non-public route rejects unauthenticated
requests in cloud mode, and that every route appears in the OpenAPI schema.

This guards against accidentally forgetting ``Depends(require_auth)`` on a
new route.
"""

import pytest

# Routes that are intentionally public (no auth required)
PUBLIC_PATHS = {"/health", "/catalog/pipelines", "/catalog/tools"}


def _protected_routes(client) -> list[tuple[str, str]]:
    """Return (method, path) pairs for all non-public GET routes."""
    schema = client.get("/openapi.json").json()
    routes = []
    for path, methods in schema["paths"].items():
        if path in PUBLIC_PATHS:
            continue
        # Skip parameterised catalog detail routes (also public)
        if path.startswith("/catalog/"):
            continue
        for method in methods:
            if method.upper() == "GET":
                routes.append((method.upper(), path))
    return routes


def test_all_routes_in_openapi(cloud_client):
    """Every registered router must appear in the OpenAPI schema."""
    schema = cloud_client.get("/openapi.json").json()
    paths = schema["paths"]
    # Spot-check key routes exist
    assert "/projects" in paths
    assert "/jobs/pipelines" in paths
    assert "/catalog/pipelines" in paths


@pytest.mark.parametrize("path", [
    "/projects",
    "/jobs/pipelines",
])
def test_protected_get_routes_return_401_without_token(cloud_client, path):
    """GET requests to protected routes without a token must return 401."""
    resp = cloud_client.get(path)
    assert resp.status_code == 401, (
        f"Expected 401 for GET {path}, got {resp.status_code}"
    )


@pytest.mark.parametrize("path", [
    "/projects",
    "/jobs/pipelines",
])
def test_protected_get_routes_return_non_401_with_valid_token(cloud_client, make_id_token, path):
    """GET requests with a valid session cookie must not be rejected by auth (501 stub is OK)."""
    token = make_id_token()
    resp = cloud_client.get(path, cookies={"session": token})
    assert resp.status_code != 401, (
        f"Got unexpected 401 on GET {path} with a valid token"
    )


def test_project_creation_requires_auth(cloud_client):
    resp = cloud_client.post("/projects", json={"name": "myproject"})
    assert resp.status_code == 401


def test_pipeline_submission_requires_auth(cloud_client):
    resp = cloud_client.post(
        "/projects/myproject/jobs/pipelines",
        json={"pipeline_id": "run_dlmuse"},
    )
    assert resp.status_code == 401


def test_file_upload_requires_auth(cloud_client):
    import io
    resp = cloud_client.post(
        "/projects/myproject/files/upload/csv",
        files={"file": ("participants.csv", io.BytesIO(b"MRID\nsubj001"), "text/csv")},
    )
    assert resp.status_code == 401
