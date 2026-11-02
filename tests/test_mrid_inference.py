"""
Tests for MRID proposals in NIfTI staging.

Two rules produce a proposal: a batch-wide shared suffix (the only one that can
recognise a non-modality suffix such as a site tag), and a per-file modality
token. Both are bounded — a shared suffix spanning more than
``MAX_SHARED_SUFFIX_TOKENS`` tokens is probably part of the ID, and any proposal
two files in a modality group would share reverts to the full stem, because
committing them would overwrite one with the other.
"""

import io
import zipfile

import pytest

from app import modalities as m
from app.services.file_service import build_nifti_proposals


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_project(client, name="mridproject"):
    resp = client.post("/projects", json={"name": name})
    assert resp.status_code == 201
    return name


def _propose(tmp_path, names, *, dirs=False):
    """Run the batch proposer over *names* and return {filename: (mrid, source)}."""
    entries = [(n, n.split("/")[-1]) for n in names] if dirs else [(n, n) for n in names]
    proposals, stripped = build_nifti_proposals(tmp_path, entries)
    return {p.filename: (p.inferred_mrid, p.mrid_source) for p in proposals}, stripped


def _nifti_upload(client, pid, names):
    files = [
        ("files", (n, io.BytesIO(b"\x00" * 16), "application/octet-stream"))
        for n in names
    ]
    resp = client.post(f"/projects/{pid}/files/upload/nifti", files=files)
    assert resp.status_code == 202, resp.text
    return resp.json()


# ── Per-file rule ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename,expected", [
    # A modality token immediately before the extension (the old behaviour).
    ("sub01_T1.nii.gz", "sub01"),
    ("sub01_T1w.nii", "sub01"),
    ("sub01_T1CE.nii.gz", "sub01"),      # longest token wins over _T1
    ("sub01_PET.nii.gz", "sub01"),
    # A hyphen is a separator too.
    ("sub01-T1.nii.gz", "sub01"),
    ("sub01-FLAIR.nii.gz", "sub01"),
    # Tokens trailing the modality token come off with it.
    ("sub01_T1_brain.nii.gz", "sub01"),
    ("sub01_FLAIR_reg.nii.gz", "sub01"),
    ("sub01-FL-reg-n4.nii.gz", "sub01"),
    # The cut goes at the LAST modality token, so an earlier one survives.
    ("sub_T1_x_T2_y.nii.gz", "sub_T1_x"),
    # A token at position 0 has no preceding separator, so nothing is stripped.
    ("T1_sub01.nii.gz", "T1_sub01"),
    # No modality token at all: only the extension comes off.
    ("sub01_MPRAGE.nii.gz", "sub01_MPRAGE"),
    ("unknown.nii.gz", "unknown"),
    # A token must end at a boundary: _T1000 is not _T1.
    ("sub01_T1000.nii.gz", "sub01_T1000"),
])
def test_per_file_rule(filename, expected):
    assert m.strip_to_mrid(filename) == expected


def test_nifti_stem_removes_only_the_extension():
    assert m.nifti_stem("sub01_T1_brain.nii.gz") == "sub01_T1_brain"
    assert m.nifti_stem("sub01_T1_brain.nii") == "sub01_T1_brain"
    assert m.nifti_stem("sub01_T1_brain") == "sub01_T1_brain"


# ── Shared-suffix rule ────────────────────────────────────────────────────────

@pytest.mark.parametrize("stems,cut", [
    # The cut moves forward to the first separator inside the common suffix:
    # these share "1_T1", which becomes "_T1".
    (["sub001_T1", "sub011_T1"], "_T1"),
    (["sub01-T1", "sub02-T1"], "-T1"),
    (["sub01_T1_brain", "sub02_T1_brain"], "_T1_brain"),
    # A site tag with no modality token — only this rule can see it.
    (["sub01_MPRAGE", "sub02_MPRAGE"], "_MPRAGE"),
    # No separator in the shared part, so there is no token boundary to cut at.
    (["abcT1", "defT1"], None),
    # Nothing shared at all.
    (["alpha", "beta"], None),
    # Spans three tokens: probably part of the ID, so rejected.
    (["IXI002-Guys-0828-T1", "IXI012-Guys-0828-T1"], None),
    # A single file is not a batch.
    (["sub01_T1"], None),
])
def test_shared_suffix_cut(stems, cut):
    assert m.shared_suffix_cut(stems) == cut


def test_shared_suffix_is_all_or_nothing_on_collision():
    # Both would collapse to "sub01", which cannot be committed to one directory.
    assert m.apply_shared_suffix(["sub01_a_T1", "sub01_b_T1"]) is not None  # distinct: sub01_a / sub01_b
    assert m.apply_shared_suffix(["sub01-T1", "sub01.T1"]) is None


def test_shared_suffix_rejected_when_it_would_empty_an_mrid():
    assert m.apply_shared_suffix(["_T1", "sub01_T1"]) is None


def test_token_cap_is_what_rejects_the_long_suffix():
    # Guard the constant itself: raising it reinstates the IXI over-strip.
    stems = ["IXI002-Guys-0828-T1", "IXI012-Guys-0828-T1"]
    assert m.MAX_SHARED_SUFFIX_TOKENS == 2
    assert m.shared_suffix_cut(stems) is None
    # The per-file rule still removes the modality token, keeping site + session.
    assert m.strip_to_mrid("IXI002-Guys-0828-T1.nii.gz") == "IXI002-Guys-0828"


# ── Batch proposals ───────────────────────────────────────────────────────────

def test_batch_strips_shared_suffix(tmp_path):
    got, stripped = _propose(tmp_path, [
        "sub01_T1_brain.nii.gz", "sub02_T1_brain.nii.gz", "sub03_T1_brain.nii.gz",
    ])
    assert got == {
        "sub01_T1_brain.nii.gz": ("sub01", "shared_suffix"),
        "sub02_T1_brain.nii.gz": ("sub02", "shared_suffix"),
        "sub03_T1_brain.nii.gz": ("sub03", "shared_suffix"),
    }
    assert stripped == {"t1": "_T1_brain"}


def test_batch_strips_site_tag_with_no_modality(tmp_path):
    got, stripped = _propose(tmp_path, ["sub01_MPRAGE.nii.gz", "sub02_MPRAGE.nii.gz"])
    assert [v[0] for v in got.values()] == ["sub01", "sub02"]
    # Modality is not inferable from _MPRAGE, so the group is keyed "".
    assert stripped == {"": "_MPRAGE"}


def test_single_file_batch_uses_the_per_file_rule(tmp_path):
    got, stripped = _propose(tmp_path, ["sub01_T1_brain.nii.gz"])
    assert got == {"sub01_T1_brain.nii.gz": ("sub01", "modality_token")}
    assert stripped == {}


def test_no_separator_in_shared_part_falls_back(tmp_path):
    got, stripped = _propose(tmp_path, ["abcT1.nii.gz", "defT1.nii.gz"])
    assert stripped == {}
    # Per-file finds no separator-preceded token either, so stems stand.
    assert got == {
        "abcT1.nii.gz": ("abcT1", "basename"),
        "defT1.nii.gz": ("defT1", "basename"),
    }


def test_zip_groups_per_modality(tmp_path):
    names = [
        "t1/sub01_T1_brain.nii.gz", "t1/sub02_T1_brain.nii.gz",
        "fl/sub01_FL_reg.nii.gz", "fl/sub02_FL_reg.nii.gz",
    ]
    got, stripped = _propose(tmp_path, names, dirs=True)
    assert stripped == {"t1": "_T1_brain", "fl": "_FL_reg"}
    assert {k: v[0] for k, v in got.items()} == {
        "sub01_T1_brain.nii.gz": "sub01", "sub02_T1_brain.nii.gz": "sub02",
        "sub01_FL_reg.nii.gz": "sub01", "sub02_FL_reg.nii.gz": "sub02",
    }


def test_one_group_strips_while_another_falls_back(tmp_path):
    # The t1 group shares a suffix; the fl group shares nothing.
    names = [
        "t1/sub01_T1_brain.nii.gz", "t1/sub02_T1_brain.nii.gz",
        "fl/sub01_FL.nii.gz", "fl/sub02_FLAIR.nii.gz",
    ]
    got, stripped = _propose(tmp_path, names, dirs=True)
    assert stripped == {"t1": "_T1_brain"}
    assert got["sub01_FL.nii.gz"] == ("sub01", "modality_token")
    assert got["sub02_FLAIR.nii.gz"] == ("sub02", "modality_token")


def test_colliding_proposals_revert_to_the_stem(tmp_path):
    # Modality-first naming: the per-file rule would make both "scan", which
    # would commit one file over the other.
    got, _ = _propose(tmp_path, ["scan_T1_001.nii.gz", "scan_T1_002.nii.gz"])
    assert got == {
        "scan_T1_001.nii.gz": ("scan_T1_001", "basename"),
        "scan_T1_002.nii.gz": ("scan_T1_002", "basename"),
    }


def test_collision_revert_leaves_other_files_alone(tmp_path):
    got, _ = _propose(tmp_path, [
        "scan_T1_001.nii.gz", "scan_T1_002.nii.gz", "sub09_T1.nii.gz",
    ])
    assert got["sub09_T1.nii.gz"] == ("sub09", "modality_token")
    assert got["scan_T1_001.nii.gz"][1] == "basename"


# ── Existing-subject matching ─────────────────────────────────────────────────

def _commit_image(project_path, modality, mrid):
    d = project_path / modality
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{mrid}.nii.gz").write_bytes(b"\x00")


def test_existing_subject_preferred_for_single_file(tmp_path):
    _commit_image(tmp_path, "t1", "sub01")
    # No modality token to go on, so only the committed subject can shorten this.
    got, _ = _propose(tmp_path, ["sub01_MPRAGE.nii.gz"])
    assert got == {"sub01_MPRAGE.nii.gz": ("sub01", "existing_subject")}


def test_per_file_rule_wins_when_it_already_agrees(tmp_path):
    _commit_image(tmp_path, "t1", "sub01")
    # Both routes give "sub01"; the reported source is the one that did the work.
    got, _ = _propose(tmp_path, ["sub01-FL-xyz.nii.gz"])
    assert got == {"sub01-FL-xyz.nii.gz": ("sub01", "modality_token")}


def test_existing_subject_longest_match_wins(tmp_path):
    _commit_image(tmp_path, "t1", "sub01")
    _commit_image(tmp_path, "t1", "sub01_visit2")
    got, _ = _propose(tmp_path, ["sub01_visit2_MPRAGE.nii.gz"])
    assert got == {"sub01_visit2_MPRAGE.nii.gz": ("sub01_visit2", "existing_subject")}


def test_existing_subject_requires_a_separator_boundary(tmp_path):
    _commit_image(tmp_path, "t1", "sub01")
    # "sub011_MPRAGE" extends "sub01" but not at a separator.
    got, _ = _propose(tmp_path, ["sub011_MPRAGE.nii.gz"])
    assert got == {"sub011_MPRAGE.nii.gz": ("sub011_MPRAGE", "basename")}


def test_existing_subject_not_applied_when_two_files_want_it(tmp_path):
    _commit_image(tmp_path, "t1", "sub01")
    # Different trailing tags, so no shared suffix and step 1 does not fire.
    # Both stems extend the committed "sub01", so taking it would collide; it
    # goes to neither rather than to whichever happens to sort first.
    got, stripped = _propose(tmp_path, ["sub01_a_MPRAGE.nii.gz", "sub01_b_SPGR.nii.gz"])
    assert stripped == {}
    assert [v[1] for v in got.values()] == ["basename", "basename"]
    assert sorted(v[0] for v in got.values()) == ["sub01_a_MPRAGE", "sub01_b_SPGR"]


def test_shared_suffix_takes_precedence_over_existing_subject(tmp_path):
    _commit_image(tmp_path, "t1", "sub01")
    got, stripped = _propose(tmp_path, ["sub01_MPRAGE.nii.gz", "sub02_MPRAGE.nii.gz"])
    assert stripped == {"": "_MPRAGE"}
    assert got["sub01_MPRAGE.nii.gz"] == ("sub01", "shared_suffix")


# ── Endpoint responses ────────────────────────────────────────────────────────

def test_upload_response_carries_the_new_fields(data_client):
    pid = _create_project(data_client)
    body = _nifti_upload(data_client, pid, ["sub01_T1_brain.nii.gz", "sub02_T1_brain.nii.gz"])
    assert body["stripped_suffixes"] == {"t1": "_T1_brain"}
    by_name = {p["filename"]: p for p in body["proposals"]}
    first = by_name["sub01_T1_brain.nii.gz"]
    assert first["inferred_mrid"] == "sub01"
    assert first["basename_mrid"] == "sub01_T1_brain"
    assert first["mrid_source"] == "shared_suffix"
    assert first["inferred_modality"] == "t1"


def test_zip_upload_response_carries_the_new_fields(data_client):
    pid = _create_project(data_client)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("t1/sub01_T1_brain.nii.gz", b"\x00" * 16)
        zf.writestr("t1/sub02_T1_brain.nii.gz", b"\x00" * 16)
    buf.seek(0)
    resp = data_client.post(
        f"/projects/{pid}/files/upload/nifti/zip",
        files={"file": ("batch.zip", buf, "application/zip")},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["stripped_suffixes"] == {"t1": "_T1_brain"}
    assert all(p["mrid_source"] == "shared_suffix" for p in body["proposals"])
    assert all(p["basename_mrid"].endswith("_T1_brain") for p in body["proposals"])


def test_proposals_stay_in_upload_order(data_client):
    pid = _create_project(data_client)
    names = ["sub03_T1.nii.gz", "sub01_T1.nii.gz", "sub02_T1.nii.gz"]
    body = _nifti_upload(data_client, pid, names)
    assert [p["filename"] for p in body["proposals"]] == names


def test_commit_still_takes_the_clients_mrid(data_client):
    pid = _create_project(data_client)
    body = _nifti_upload(data_client, pid, ["sub01_T1_brain.nii.gz"])
    resp = data_client.post(
        f"/projects/{pid}/files/stage/{body['staging_id']}/commit",
        json={"mappings": [{
            "filename": "sub01_T1_brain.nii.gz",
            "mrid": "whatever_the_user_typed",
            "modality": "t1",
        }]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["committed"][0]["mrid"] == "whatever_the_user_typed"
