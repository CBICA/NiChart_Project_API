"""
Subject coverage of pipeline steps: dropped subjects detected from declared
outputs, traced downstream, an all-excluded step failing distinctly, and the
staged publishing that guarantees outputs come from the run that wrote them.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
from pathlib import Path

import pytest

from app.backends.base import JobBackend, JobHandle, ManifestSpec
from app.models.catalog import PipelineStep
from app.services import job_service, readiness_service, step_ledger, subject_coverage

SEG_TOOL = """
name: Seg tool
description: test
inputs:
  t1:
    type: directory
outputs:
  seg:
    type: directory
    contents:
      - pattern: "{MRID}_seg.nii.gz"
        per_subject: true
  vol:
    type: directory
    merge: directory_union_csv_concat
    contents:
      - pattern: "Vol.csv"
        csv_columns: [MRID]
        rows_per_subject: true
    manifest:
      pattern: "qc.csv"
      mrid_column: MRID
      reason_column: Reason
mounts:
  t1: {path_in_container: /in, mode: ro}
  seg: {path_in_container: /seg, mode: rw}
  vol: {path_in_container: /vol, mode: rw}
parameters: {}
resources: {vcpus: 1, memory: 100, gpus: 0}
parallelizable: PARALLEL
subjects_per_chunk: 2
container: {image: test/seg:1, command: run}
"""

HARM_TOOL = """
name: Harm tool
description: test
inputs:
  vol_csv: {type: file, csv_columns: [MRID]}
  covariates: {type: file, csv_columns: [MRID]}
outputs:
  out_csv:
    type: file
    rows_per_subject: true
    manifest: "log.txt"
mounts:
  vol_csv: {path_in_container: /in/vol.csv, mode: ro}
  covariates: {path_in_container: /in/cov.csv, mode: ro}
  out_csv: {path_in_container: /out/out.csv, mode: rw}
parameters: {}
resources: {vcpus: 1, memory: 100, gpus: 0}
container: {image: test/harm:1, command: run}
"""

BLIND_TOOL = """
name: Blind tool
description: declares nothing about its outputs
inputs:
  t1: {type: directory}
outputs:
  out: {type: directory}
mounts:
  t1: {path_in_container: /in, mode: ro}
  out: {path_in_container: /out, mode: rw}
parameters: {}
resources: {vcpus: 1, memory: 100, gpus: 0}
container: {image: test/blind:1, command: run}
"""

STEPS = [
    PipelineStep(id="seg", tool="seg_tool",
                 inputs={"t1": "${STUDY}/t1"},
                 outputs={"seg": "${STUDY}/seg", "vol": "${STUDY}/vol"}),
    PipelineStep(id="harm", tool="harm_tool",
                 inputs={"vol_csv": "${STUDY}/vol/Vol.csv",
                         "covariates": "${STUDY}/participants/participants.csv"},
                 outputs={"out_csv": "${STUDY}/harm/out.csv"}),
]


class _Handle(JobHandle):
    def __init__(self, ok: bool = True):
        self._ok = ok

    @property
    def job_id(self) -> str:
        return "job-1"

    async def status(self) -> str:
        return "succeeded" if self._ok else "failed"

    async def logs(self, tail: int = 200) -> str:
        return ""

    async def cancel(self) -> None:
        pass


def _stems(d: Path) -> list[str]:
    return sorted(p.name.split(".nii")[0] for p in d.iterdir() if ".nii" in p.name)


def _segs(d: Path) -> list[str]:
    return sorted(p.name[: -len("_seg.nii.gz")] for p in d.iterdir() if p.name.endswith("_seg.nii.gz"))


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


class _ScriptedBackend(JobBackend):
    """Writes each tool's outputs into the mounted paths, dropping chosen subjects.

    ``drop[tool]`` = {mrid: reason or None}; a reason goes into the tool's QC
    manifest, None drops silently. ``fail`` = tools whose job fails.
    """

    def __init__(self, drop: dict | None = None, fail: set | None = None):
        self.drop = drop or {}
        self.fail = fail or set()
        self.mounts: list[dict] = []

    async def submit(self, tool_spec, mount_paths, params, num_subjects=1,
                     user_token=None, extra_readonly_mounts=None, study_id=None):
        self.mounts.append(dict(mount_paths))
        tool = tool_spec.tool_id
        if tool in self.fail:
            return _Handle(ok=False)
        drop = self.drop.get(tool, {})
        if tool == "seg_tool":
            subs = _stems(Path(mount_paths["t1"]))
            keep = [m for m in subs if m not in drop]
            seg, vol = Path(mount_paths["seg"]), Path(mount_paths["vol"])
            for m in keep:
                (seg / f"{m}_seg.nii.gz").write_bytes(b"seg")
            _write_csv(vol / "Vol.csv", ["MRID", "V"], [[m, "1"] for m in keep])
            listed = [[m, r] for m, r in drop.items() if r and m in subs]
            if listed:
                _write_csv(vol / "qc.csv", ["MRID", "Reason"], listed)
        elif tool == "harm_tool":
            with open(mount_paths["vol_csv"], newline="") as f:
                subs = [r["MRID"] for r in csv.DictReader(f)]
            with open(mount_paths["covariates"], newline="") as f:
                have = {r["MRID"] for r in csv.DictReader(f)}
            subs = [m for m in subs if m in have]   # needs covariates
            out = Path(mount_paths["out_csv"])
            _write_csv(out, ["MRID", "H"], [[m, "2"] for m in subs if m not in drop])
            (out.parent / "log.txt").write_text("harmonized\nall good\n")
        elif tool == "blind_tool":
            Path(mount_paths["out"], "whatever.txt").write_text("x")
        return _Handle()


@pytest.fixture
def env(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "seg_tool.yaml").write_text(SEG_TOOL.replace("PARALLEL", "false"))
    (tools / "harm_tool.yaml").write_text(HARM_TOOL)
    (tools / "blind_tool.yaml").write_text(BLIND_TOOL)
    data = tmp_path / "data"
    study = data / "LOCAL_USER" / "proj"
    (study / "t1").mkdir(parents=True)
    for m in ("S1", "S2", "S3", "S4"):
        (study / "t1" / f"{m}.nii.gz").write_bytes(b"img")
    _write_csv(study / "participants" / "participants.csv", ["MRID", "Age"],
               [[m, "70"] for m in ("S1", "S2", "S3", "S4")])
    job_service._data_root = data
    return {"tools": tools, "study": study, "data": data}


def _run(env, backend, steps=STEPS, scope=("S1", "S2", "S3", "S4")):
    run = job_service.create_pipeline_run(
        "LOCAL_USER", "proj", "test_pl", steps, scope=list(scope) if scope else None
    )
    asyncio.run(job_service.run_pipeline_task(
        run=run, pipeline_steps=steps, study_dir=env["study"], backend=backend,
        user_params={}, reuse_cached_steps=True, user_token=None,
        tools_path=env["tools"],
    ))
    return job_service.get_run_detail(run.run_id, "LOCAL_USER")


# ── End to end ────────────────────────────────────────────────────────────────

def test_partial_drop_is_detected_explained_and_traced_downstream(env):
    detail = _run(env, _ScriptedBackend(drop={"seg_tool": {"S2": "Failed QC", "S3": None}}))

    assert detail.status == "succeeded"
    assert detail.partial_success is True
    assert detail.scans_total == 4
    assert detail.scans_incomplete == 2

    seg = detail.steps[0].coverage
    assert seg.checked and seg.expected == 4 and seg.produced == 2
    by = {d.mrid: d for d in seg.dropped}
    assert by["S2"].source == "manifest" and by["S2"].reason == "Failed QC"
    assert by["S3"].source == "no_output"
    assert "seg/S3_seg.nii.gz" in by["S3"].reason and "a row in vol/Vol.csv" in by["S3"].reason
    assert [m.path for m in seg.manifests] == ["vol/qc.csv"]
    assert "Failed QC" in seg.manifests[0].excerpt

    harm = detail.steps[1].coverage
    assert harm.checked and harm.expected == 2 and harm.produced == 2 and not harm.dropped
    origins = {m.mrid: m.origin for m in harm.missing_inputs}
    assert set(origins) == {"S2", "S3"}
    assert origins["S2"].kind == "dropped" and origins["S2"].step_id == "seg"
    assert origins["S2"].reason == "Failed QC"
    assert origins["S2"].input == "vol/Vol.csv"
    # A manifest in an unknown format is still recorded, as written.
    assert harm.manifests[0].path == "harm/log.txt"
    assert harm.manifests[0].excerpt == "harmonized\nall good\n"

    study = env["study"]
    assert _segs(study / "seg") == ["S1", "S4"]
    assert (study / "harm" / "log.txt").exists()
    assert not (study / "_working" / "steps").exists() or not any((study / "_working" / "steps").iterdir())
    ledger = step_ledger.load(study)
    rec = ledger["executions"][ledger["outputs"]["seg"]]
    assert rec["coverage"]["produced"] == 2


def test_job_writes_to_staging_not_published_paths(env):
    backend = _ScriptedBackend()
    _run(env, backend)
    seg_mounts = backend.mounts[0]
    assert "/_working/steps/" in seg_mounts["seg"]
    assert seg_mounts["t1"].endswith("/proj/t1")
    assert "/_working/steps/" in backend.mounts[1]["out_csv"]
    assert backend.mounts[1]["out_csv"].endswith("/harm/out_csv/out.csv")  # step/label/name


def test_stale_outputs_from_an_earlier_run_cannot_hide_a_drop(env):
    study = env["study"]
    (study / "seg").mkdir()
    for m in ("S1", "S2", "S3", "S4", "OLD"):
        (study / "seg" / f"{m}_seg.nii.gz").write_bytes(b"old")

    detail = _run(env, _ScriptedBackend(drop={"seg_tool": {"S4": None}}))

    assert [d.mrid for d in detail.steps[0].coverage.dropped] == ["S4"]
    assert _segs(study / "seg") == ["S1", "S2", "S3"]   # old S4 and OLD are gone
    rec = step_ledger.load(study)["executions"][step_ledger.load(study)["outputs"]["seg"]]
    assert {"label": "seg", "path": "seg", "source": "unknown"} in rec["replaced"]


def test_rerun_records_which_execution_it_replaced(env):
    first = _run(env, _ScriptedBackend())
    run2 = job_service.create_pipeline_run(
        "LOCAL_USER", "proj", "test_pl", STEPS, scope=["S1", "S2", "S3", "S4"]
    )
    asyncio.run(job_service.run_pipeline_task(
        run=run2, pipeline_steps=STEPS, study_dir=env["study"], backend=_ScriptedBackend(),
        user_params={}, reuse_cached_steps=False, user_token=None, tools_path=env["tools"],
    ))
    ledger = step_ledger.load(env["study"])
    rec = ledger["executions"][ledger["outputs"]["seg"]]
    assert rec["run_id"] == run2.run_id
    replaced = {r["label"]: r for r in rec["replaced"]}
    assert replaced["seg"]["source"] == "step"
    assert replaced["seg"]["run_id"] == first.run_id


def test_all_excluded_fails_distinctly_and_keeps_previous_outputs(env):
    study = env["study"]
    (study / "seg").mkdir()
    (study / "seg" / "S1_seg.nii.gz").write_bytes(b"previous")

    detail = _run(env, _ScriptedBackend(drop={"seg_tool": {m: "bad" for m in ("S1", "S2", "S3", "S4")}}))

    assert detail.status == "failed"
    step = detail.steps[0]
    assert step.status == "failed"
    assert step.failure_kind == "all_excluded"
    assert step.coverage.all_excluded and step.coverage.produced == 0
    assert "All 4 scans were excluded" in step.error
    assert "vol/qc.csv" in step.error
    assert detail.steps[1].status == "pending"
    assert (study / "seg" / "S1_seg.nii.gz").read_bytes() == b"previous"
    assert not (study / "_working" / "steps").exists() or not any((study / "_working" / "steps").iterdir())


def test_a_failed_job_is_an_error_and_publishes_nothing(env):
    study = env["study"]
    (study / "seg").mkdir()
    (study / "seg" / "S1_seg.nii.gz").write_bytes(b"previous")

    detail = _run(env, _ScriptedBackend(fail={"seg_tool"}))

    assert detail.steps[0].status == "failed"
    assert detail.steps[0].failure_kind == "error"
    assert detail.partial_success is False
    assert _segs(study / "seg") == ["S1"]


def test_undeclared_outputs_are_reported_unchecked(env):
    steps = [PipelineStep(id="blind", tool="blind_tool",
                          inputs={"t1": "${STUDY}/t1"}, outputs={"out": "${STUDY}/blind"})]
    detail = _run(env, _ScriptedBackend(), steps=steps)
    cov = detail.steps[0].coverage
    assert detail.status == "succeeded"
    assert cov.checked is False and "contents" in cov.note
    assert detail.partial_success is False
    assert (env["study"] / "blind" / "whatever.txt").exists()


def test_missing_participants_row_is_traced_to_the_input(env):
    study = env["study"]
    _write_csv(study / "participants" / "participants.csv", ["MRID", "Age"],
               [[m, "70"] for m in ("S1", "S2", "S3")])
    detail = _run(env, _ScriptedBackend())
    origins = {m.mrid: m.origin for m in detail.steps[1].coverage.missing_inputs}
    assert origins["S4"].kind == "not_in_input"
    assert origins["S4"].input == "participants/participants.csv"


def test_chunked_step_coverage_is_independent_of_chunks(env):
    (env["tools"] / "seg_tool.yaml").write_text(SEG_TOOL.replace("PARALLEL", "true"))
    detail = _run(env, _ScriptedBackend(drop={"seg_tool": {"S3": "QC"}}))
    seg = detail.steps[0]
    assert seg.total_chunks == 2 and seg.failed_chunks == 0
    assert seg.coverage.expected == 4 and seg.coverage.produced == 3
    assert [d.mrid for d in seg.coverage.dropped] == ["S3"]
    assert _segs(env["study"] / "seg") == ["S1", "S2", "S4"]


def test_cached_step_carries_the_coverage_it_ran_with(env):
    _run(env, _ScriptedBackend(drop={"seg_tool": {"S2": "QC"}}))
    detail = _run(env, _ScriptedBackend())
    assert detail.steps[0].skip_reason == "cached"
    assert [d.mrid for d in detail.steps[0].coverage.dropped] == ["S2"]
    assert detail.partial_success is True


def test_without_a_scope_the_first_checked_step_defines_it(env):
    detail = _run(env, _ScriptedBackend(drop={"seg_tool": {"S2": None}}), scope=None)
    assert detail.scans_total == 4
    assert [m.mrid for m in detail.steps[1].coverage.missing_inputs] == ["S2"]


def test_publish_rolls_back_when_a_move_fails(env, monkeypatch):
    study = env["study"]
    (study / "seg").mkdir()
    (study / "seg" / "S1_seg.nii.gz").write_bytes(b"previous")
    staged = job_service._staged_outputs(study, "r1", "seg", {"seg": str(study / "seg"), "vol": str(study / "vol")})
    job_service._prepare_staging(study, "r1", "seg", staged)
    Path(staged["seg"], "S1_seg.nii.gz").write_bytes(b"new")

    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:   # after moving the old seg aside and the new one in
            raise OSError("disk trouble")
        return real(src, dst)

    monkeypatch.setattr(job_service.os, "replace", flaky)
    with pytest.raises(OSError):
        job_service._publish_outputs(study, "r1", "seg", staged,
                                     {"seg": str(study / "seg"), "vol": str(study / "vol")})
    monkeypatch.setattr(job_service.os, "replace", real)
    assert (study / "seg" / "S1_seg.nii.gz").read_bytes() == b"previous"


# ── Units ─────────────────────────────────────────────────────────────────────

def test_pattern_regex_captures_the_mrid():
    rx = subject_coverage.pattern_regex("{MRID}_DLMUSE.nii.gz")
    assert rx.fullmatch("sub-01_ses-1_DLMUSE.nii.gz").group("mrid") == "sub-01_ses-1"
    assert rx.fullmatch("sub01_DLMUSExnii.gz") is None
    rx2 = subject_coverage.pattern_regex("*/{MRID}.nii.gz")
    assert rx2.fullmatch("a/S1.nii.gz").group("mrid") == "S1"


def test_csv_column_reading_handles_bom_case_and_blanks(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes("﻿mrid,Age\nA,1\n,2\n B ,3\n".encode("utf-8"))
    assert subject_coverage.read_csv_column(p, "MRID") == {"A", "B"}
    assert subject_coverage.read_csv_column(p, "Nope") is None
    assert subject_coverage.read_csv_column(tmp_path / "missing.csv") is None


def test_manifest_rows_without_a_reason_column_use_the_whole_row(tmp_path):
    p = tmp_path / "m.csv"
    _write_csv(p, ["MRID", "Stage", "Score"], [["S1", "qc", "0.2"]])
    got = subject_coverage.read_manifest_reasons(p, ManifestSpec(pattern="m.csv", mrid_column="MRID"))
    assert got == {"S1": "Stage: qc; Score: 0.2"}
    assert subject_coverage.read_manifest_reasons(p, ManifestSpec(pattern="m.csv")) is None


def test_binary_manifest_is_offered_without_an_excerpt(tmp_path):
    p = tmp_path / "m.bin"
    p.write_bytes(b"\x00\x01\x02")
    assert subject_coverage.manifest_excerpt(p) == {"bytes": 3, "excerpt": None, "truncated": True}


def test_input_subjects_never_guess_unknown_naming(tmp_path):
    d = tmp_path / "masks"
    d.mkdir()
    (d / "S1_mask.nii.gz").write_bytes(b"")
    assert subject_coverage.input_slot_subjects(d, None, None) is None
    t1 = tmp_path / "t1"
    t1.mkdir()
    (t1 / "S1.nii.gz").write_bytes(b"")
    (t1 / "S2.nii").write_bytes(b"")
    assert subject_coverage.input_slot_subjects(t1, None, None) == {"S1", "S2"}


def test_pipeline_subjects_is_the_complete_sets(tmp_path):
    for mod, ms in (("t1", ["A", "B", "C"]), ("fl", ["B", "C", "D"])):
        (tmp_path / mod).mkdir()
        for m in ms:
            (tmp_path / mod / f"{m}.nii.gz").write_bytes(b"")
    assert readiness_service.pipeline_subjects(tmp_path, ["needs_T1", "needs_FLAIR"]) == {"B", "C"}
    assert readiness_service.pipeline_subjects(tmp_path, ["needs_demographics"]) is None


def test_old_run_records_without_coverage_still_load(env):
    run = job_service.create_pipeline_run("LOCAL_USER", "proj", "test_pl", STEPS)
    d = json.loads(job_service._run_to_json(run)) if hasattr(job_service, "_run_to_json") else job_service._run_to_dict(run)
    d.pop("scope", None)
    for s in d["steps"]:
        s.pop("coverage", None)
        s.pop("failure_kind", None)
    back = job_service._run_from_dict(d)
    assert back.scope is None and back.steps[0].coverage is None
    assert job_service._to_summary(back).scans_incomplete == 0


# ── Cloud: making S3 match the published outputs ──────────────────────────────

class _KeyStore:
    """Minimal S3 stand-in: a key → bytes map with prefix listing, copy, delete."""

    def __init__(self, keys: dict[str, bytes]):
        self.keys = dict(keys)

    def get_paginator(self, _name):
        store = self

        class _P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [
                    {"Key": k, "Size": len(v), "ETag": '""'}
                    for k, v in sorted(store.keys.items()) if k.startswith(Prefix)
                ]}
        return _P()

    def copy(self, src, bucket, key):
        self.keys[key] = self.keys[src["Key"]]

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.keys.pop(o["Key"], None)


def test_promote_replaces_published_objects_and_removes_staging(tmp_path, monkeypatch):
    from app.services import s3_sync_service

    pre = "fsx/U/proj"
    store = _KeyStore({
        f"{pre}/_working/steps/r/seg/seg/S1_seg.nii.gz": b"new1",
        f"{pre}/_working/steps/r/seg/seg/S2_seg.nii.gz": b"new2",
        f"{pre}/seg/S1_seg.nii.gz": b"old1",
        f"{pre}/seg/S9_seg.nii.gz": b"stale",          # replaced dir: must go
        f"{pre}/_working/steps/r/harm/out/out.csv": b"csv",
        f"{pre}/harm/out.csv": b"oldcsv",
        f"{pre}/harm/keep.txt": b"keep",              # beside a file output: stays
    })
    monkeypatch.setattr(s3_sync_service.boto3, "client", lambda svc, **kw: store)
    (tmp_path / "seg").mkdir()
    for m in ("S1", "S2"):
        (tmp_path / "seg" / f"{m}_seg.nii.gz").write_bytes(b"x")

    asyncio.run(s3_sync_service.promote_outputs("b", pre, tmp_path, [
        ("_working/steps/r/seg/seg", "seg", True),
        ("_working/steps/r/harm/out", "harm", False),
    ]))

    assert store.keys == {
        f"{pre}/seg/S1_seg.nii.gz": b"new1",
        f"{pre}/seg/S2_seg.nii.gz": b"new2",
        f"{pre}/harm/out.csv": b"csv",
        f"{pre}/harm/keep.txt": b"keep",
    }


# ── Provenance report ─────────────────────────────────────────────────────────

PIPELINE_YAML = """
pipeline_name: Test PL
description: test
requires: [needs_T1]
steps:
  - id: seg
    tool: seg_tool
    inputs: {t1: "${STUDY}/t1"}
    outputs: {seg: "${STUDY}/seg", vol: "${STUDY}/vol"}
  - id: harm
    tool: harm_tool
    inputs:
      vol_csv: "${STUDY}/vol/Vol.csv"
      covariates: "${STUDY}/participants/participants.csv"
    outputs: {out_csv: "${STUDY}/harm/out.csv"}
results:
  batch_features:
    file: "harm/out.csv"
    mrid_column: MRID
"""


def test_provenance_report_shows_coverage_and_missing_scans(env, tmp_path):
    from app.services import provenance_report

    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "test_pl.yaml").write_text(PIPELINE_YAML)
    resources = tmp_path / "resources"
    resources.mkdir()

    _run(env, _ScriptedBackend(drop={"seg_tool": {"S2": "Failed QC"}}))
    dossier = provenance_report.build_dossier(
        env["study"], "proj", "LOCAL_USER", pipelines, env["tools"], resources,
    )
    seg = next(e for e in dossier.executions if e.step_id == "seg")
    assert [d.mrid for d in seg.coverage.dropped] == ["S2"]
    assert seg.coverage_measured_now is False
    assert any("Dropped 1 of its 4 scans" in w for w in seg.warnings)
    result = next(r for r in dossier.results if r.pipeline_id == "test_pl")
    assert [(m.mrid, m.origin.kind, m.origin.step_id) for m in result.missing_scans] == [
        ("S2", "dropped", "seg"),
    ]
    assert any("cover every scan" in w for w in dossier.warnings)


def test_provenance_measures_coverage_now_for_steps_recorded_before_it(env, tmp_path):
    from app.services import provenance_report

    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "test_pl.yaml").write_text(PIPELINE_YAML)
    (tmp_path / "resources").mkdir()
    _run(env, _ScriptedBackend(drop={"seg_tool": {"S3": None}}))
    # Strip what an older server wouldn't have recorded.
    path = env["study"] / "_working" / "step_ledger.json"
    ledger = json.loads(path.read_text())
    for rec in ledger["executions"].values():
        rec.pop("coverage", None)
    path.write_text(json.dumps(ledger))

    dossier = provenance_report.build_dossier(
        env["study"], "proj", "LOCAL_USER", pipelines, env["tools"], tmp_path / "resources",
    )
    seg = next(e for e in dossier.executions if e.step_id == "seg")
    assert seg.coverage_measured_now is True
    assert [d.mrid for d in seg.coverage.dropped] == ["S3"]
