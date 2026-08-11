"""
Tests for the central modality registry (app.modalities) and its endpoint.

These lock in the behaviors every consumer relies on, so adding a modality is a
one-line registry change that stays correct across uploads, readiness, and clients.
"""

from app import modalities as m


def test_inference_order_and_strip():
    assert m.infer_modality("sub-01_T1.nii.gz") == "t1"
    assert m.infer_modality("sub-01_T1CE.nii.gz") == "t1ce"   # t1ce must win over t1
    assert m.infer_modality("x_FLAIR.nii.gz") == "fl"
    assert m.infer_modality("x_T1w.nii.gz") == "t1"
    assert m.infer_modality("x_PET.nii.gz") == "pet"
    assert m.infer_modality("x_nothing.nii.gz") is None

    assert m.strip_to_mrid("sub-01_T1CE.nii.gz") == "sub-01"
    assert m.strip_to_mrid("sub-01_PET.nii.gz") == "sub-01"
    assert m.strip_to_mrid("sub-01.nii.gz") == "sub-01"


def test_requires_tokens():
    assert m.modality_for_requires("needs_t1") == "t1"
    assert m.modality_for_requires("needs_FLAIR") == "fl"      # alias, case-insensitive
    assert m.modality_for_requires("needs_pet") == "pet"
    assert m.modality_for_requires("needs_nope") is None


def test_validity():
    assert m.is_valid("pet")
    assert not m.is_valid("xyz")


def test_modalities_endpoint(local_client):
    r = local_client.get("/catalog/modalities")
    assert r.status_code == 200
    codes = {x["code"] for x in r.json()}
    assert {"t1", "fl", "t2", "t1ce", "adc", "pet"} <= codes
    # each entry carries the study subdir
    assert all("dir" in x and "label" in x for x in r.json())


def test_commit_rejects_unknown_modality(data_client):
    data_client.post("/projects", json={"name": "modtest"})
    r = data_client.post(
        "/projects/modtest/files/stage/nope/commit",
        json={"mappings": [{"filename": "a.nii.gz", "mrid": "a", "modality": "xyz"}]},
    )
    assert r.status_code == 422
