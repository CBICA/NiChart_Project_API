"""Tests for the /health endpoint."""


def test_health_local(local_client):
    resp = local_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["execution_mode"] == "local"
    assert "version" in body


def test_health_no_auth_required(cloud_client):
    """Health must be reachable in cloud mode without a token."""
    resp = cloud_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["execution_mode"] == "cloud"


def test_health_appears_in_openapi(local_client):
    schema = local_client.get("/openapi.json").json()
    assert "/health" in schema["paths"]
