"""Provenance report: ingest log, step ledger, legacy reconstruction, replication commands."""

import gzip
import json
import shlex

from app.services import ingest_log, replication

PROJECT = "provproj"


def _make_project(client, name=PROJECT):
    assert client.post("/projects", json={"name": name}).status_code == 201
    return name


def _nifti_bytes(seed: int = 0) -> bytes:
    return gzip.compress(bytes([seed]) * 400)


def _upload_nifti(client, names, project=PROJECT):
    files = [("files", (n, _nifti_bytes(i), "application/gzip")) for i, n in enumerate(names)]
    staged = client.post(f"/projects/{project}/files/upload/nifti", files=files)
    assert staged.status_code == 202, staged.text
    body = staged.json()
    mappings = [
        {"filename": p["filename"], "mrid": p["filename"].split("_")[0], "modality": "t1"}
        for p in body["proposals"]
    ]
    resp = client.post(
        f"/projects/{project}/files/stage/{body['staging_id']}/commit",
        json={"mappings": mappings},
    )
    assert resp.status_code == 200, resp.text


def _report(client, project=PROJECT):
    resp = client.get(f"/projects/{project}/provenance/report")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── Ingest log ────────────────────────────────────────────────────────────────

def test_upload_is_recorded_with_its_files(data_client, tmp_path):
    _make_project(data_client)
    _upload_nifti(data_client, ["Subj1_T1.nii.gz", "Subj2_T1.nii.gz"])

    report = _report(data_client)
    uploads = [e for e in report["events"] if e["kind"] == "nifti_upload"]
    assert len(uploads) == 1
    assert set(uploads[0]["files"]) >= {"t1/Subj1.nii.gz", "t1/Subj2.nii.gz"}
    # participants.csv rows added by the same request belong to the same event.
    assert "participants" in uploads[0]["roots"]
    t1 = next(d for d in report["inputs"] if d["path"] == "t1")
    assert t1["files"] == 2 and t1["untracked_files"] == 0


def test_existing_inputs_become_a_baseline_and_outside_edits_are_untracked(data_client, tmp_path):
    _make_project(data_client)
    study = tmp_path / "LOCAL_USER" / PROJECT
    (study / "t1").mkdir()
    (study / "t1" / "A.nii.gz").write_bytes(_nifti_bytes(1))

    first = _report(data_client)
    assert [e["kind"] for e in first["events"]] == ["baseline"]
    assert first["events"][0]["earliest_mtime"]
    assert first["inputs"][0]["untracked_files"] == 1

    (study / "t1" / "A.nii.gz").write_bytes(_nifti_bytes(2))
    ingest_log.begin(study)
    kinds = [e["kind"] for e in ingest_log.load(study)["events"]]
    assert kinds == ["baseline", "untracked_change"]


def test_deleting_an_input_is_recorded(data_client):
    _make_project(data_client)
    _upload_nifti(data_client, ["Subj1_T1.nii.gz"])
    assert data_client.delete(
        f"/projects/{PROJECT}/files", params={"path": "t1/Subj1.nii.gz"}
    ).status_code == 204
    deletes = [e for e in _report(data_client)["events"] if e["kind"] == "file_delete"]
    assert deletes and deletes[0]["removed"] == 1


# ── Step ledger ───────────────────────────────────────────────────────────────

def test_executed_step_is_reported_with_commands_and_checksums(job_client):
    _make_project(job_client)
    run = job_client.post(
        f"/projects/{PROJECT}/jobs/pipelines",
        json={"pipeline_id": "test_pipeline", "params": {"duration_seconds": 3}},
    ).json()["run_id"]

    report = _report(job_client)
    (ex,) = report["executions"]
    assert ex["execution_id"] == f"{run}:sleep"
    assert ex["reconstructed"] is False
    assert ex["image"] == "alpine:3.19"
    assert ex["params"] == {"duration_seconds": 3}
    assert "sleep 3" in ex["command"]
    assert [o["path"] for o in ex["outputs"]] == ["test_output"]
    assert ex["warnings"] == []
    assert '"${NICHART_PROJECT}/test_output:/output:rw"' in ex["commands"]["docker"]
    assert ex["commands"]["singularity"].splitlines()[-4].startswith("apptainer exec")
    assert "docker pull alpine:3.19" in report["setup"]["docker"]

    sums = job_client.get(f"/projects/{PROJECT}/provenance/checksums")
    assert sums.status_code == 200
    assert "attachment" in sums.headers["content-disposition"]


def test_output_edited_after_the_step_is_flagged(job_client, tmp_path):
    _make_project(job_client)
    job_client.post(
        f"/projects/{PROJECT}/jobs/pipelines",
        json={"pipeline_id": "test_pipeline", "params": {"duration_seconds": 1}},
    )
    (tmp_path / "LOCAL_USER" / PROJECT / "test_output" / "edited.txt").write_text("by hand")
    (ex,) = _report(job_client)["executions"]
    assert any("has changed since this step wrote it" in w for w in ex["warnings"])


# ── Legacy reconstruction ─────────────────────────────────────────────────────

def test_legacy_provenance_is_reconstructed_where_the_files_really_are(data_client, tmp_path):
    """A _provenance.json from before the ledger: run_spare_all's definition now
    writes spare_all/SPARE_ALL.csv, but this run wrote ml_biomarkers/."""
    _make_project(data_client)
    study = tmp_path / "LOCAL_USER" / PROJECT
    (study / "ml_biomarkers").mkdir()
    (study / "ml_biomarkers" / "SPARE_ALL.csv").write_text("MRID,SPARE\nA,1\n")
    (study / "ml_biomarkers" / "_provenance.json").write_text(json.dumps({
        "generated_at": "2026-11-06T20:43:40+00:00",
        "pipeline_id": "run_spare_all",
        "step_id": "run_spare_all",
        "container_image": "cbica/nichart_spare_score_runall:0.0.1",
        "params": {},
        "input_paths": {
            "dlmuse_vol": str(study / "dlmuse_vol" / "DLMUSE_Volumes.csv"),
            "demog_csv": str(study / "participants" / "participants.csv"),
        },
    }))

    (ex,) = _report(data_client)["executions"]
    assert ex["reconstructed"] is True
    assert [o["path"] for o in ex["outputs"]] == ["ml_biomarkers/SPARE_ALL.csv"]
    docker = ex["commands"]["docker"]
    assert '"${NICHART_PROJECT}/ml_biomarkers:/output:rw"' in docker
    # The command uses the image the step ran, not the tool's current one.
    assert "cbica/nichart_spare_score_runall:0.0.1" in docker
    assert any("the image this step actually ran" in w for w in ex["warnings"])
    assert {s["kind"] for s in ex["inputs"]} == {"unknown", "input"}


# ── Replication commands ──────────────────────────────────────────────────────

_REC = {
    "image": "cbica/tool:1.0",
    "image_digest": "cbica/tool@sha256:abc",
    "command": "-i /input/a.csv -o '/output/x y.csv'",
    "gpus": True,
    "singularity_run_mode": None,
    "inputs": {"in_csv": "dlmuse_vol/DLMUSE_Volumes.csv", "seg": "dlmuse_seg"},
    "outputs": {"out": "results/x y.csv"},
    "mounts": {
        "in_csv": {"container_path": "/input/a.csv", "mode": "ro", "mount_type": "input_file"},
        "seg": {"container_path": "/input/seg", "mode": "ro", "mount_type": "directory"},
        "out": {"container_path": "/output/x y.csv", "mode": "rw", "mount_type": "output_file"},
    },
}


def test_docker_command_mirrors_the_backend_mounts():
    cmd = replication.docker_command(_REC)
    assert cmd.startswith('mkdir -p "${NICHART_PROJECT}/results"\n')
    assert '-v "${NICHART_PROJECT}/dlmuse_vol/DLMUSE_Volumes.csv:/input/a.csv:ro"' in cmd
    assert '-v "${NICHART_PROJECT}/results:/output:rw"' in cmd       # parent of an output file
    assert "--gpus all" in cmd
    assert "cbica/tool:1.0@sha256:abc" in cmd                        # pinned by digest
    # The command survives shell parsing as the same tokens.
    last = cmd.splitlines()[-1].strip()
    assert shlex.split(last) == ["-i", "/input/a.csv", "-o", "/output/x y.csv"]


def test_singularity_command_uses_run_mode_and_sif_name():
    cmd = replication.singularity_command(_REC)
    assert "apptainer run --writable-tmpfs --nv" in cmd             # args-only command → run
    assert "cbica_tool_1.0.sif" in cmd
    setup = replication.setup_commands([("cbica/tool:1.0", "cbica/tool@sha256:abc")])
    assert setup["singularity"] == (
        "apptainer pull cbica_tool_1.0.sif docker://cbica/tool:1.0@sha256:abc"
    )


def test_project_paths_are_escaped_inside_double_quotes():
    assert replication.host_path('we"ird$dir') == '"${NICHART_PROJECT}/we\\"ird\\$dir"'
