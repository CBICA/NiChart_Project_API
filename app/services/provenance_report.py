"""
Project provenance report: how every current result was produced, from upload
onwards, and the commands to reproduce it.

Sources, newest first:

* ``step_ledger`` — a full record per executed step (command, mounts, digests).
* ``_provenance.json`` in output directories, for steps that ran before the
  ledger existed. These are *reconstructed*: the image, params and inputs come
  from the file, the command and mounts from the current tool definition, the
  output paths from the current pipeline definition.
* ``ingest_log`` — every upload/edit of raw inputs. Inputs present before it
  existed are a ``baseline`` event, dated by file modification time.
* Run records (``job_service``) — to tie a step to its run, image digest and
  chunking, and a DICOM conversion to its converter image.

The report only describes what is on disk now: a step whose outputs have all
been overwritten since is not in it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from app.models.provenance import (
    DatasetDigest,
    ExecutionOut,
    IngestEventOut,
    InputDataset,
    InputSource,
    ProvenanceDossier,
    ResultLineage,
)
from app.services import (
    catalog_service,
    file_hashes,
    ingest_log,
    job_service,
    manual_upload_store,
    replication,
    results_service,
    step_ledger,
)
from app.services.provenance_service import _check_input

_log = logging.getLogger(__name__)

_EXCLUDE_DIRS = {"_upload", "_working"}
# Seconds of slack when matching a _provenance.json to its run step, and when
# deciding a file was written after its step finished.
_TOLERANCE = 2.0
_EVENT_FILES_SHOWN = 50

NOTES = [
    "Commands assume a copy of the project at $NICHART_PROJECT holding the input files listed "
    "under Inputs. Check them against the checksums file (sha256sum -c) before running.",
    "Results can differ slightly when rerun on different hardware (e.g. GPU vs CPU, or another "
    "GPU model): deep-learning tools are not always bit-for-bit deterministic.",
]


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _top(rel: str) -> str:
    return rel.split("/", 1)[0]


def _related(a: str, b: str) -> bool:
    """Same path, or one inside the other."""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


class _Builder:
    def __init__(
        self,
        study_dir: Path,
        project_id: str,
        user_id: str,
        pipelines_path: Path,
        tools_path: Path,
        resources_path: Path,
    ) -> None:
        self.study_dir = study_dir
        self.project_id = project_id
        self.pipelines_path = pipelines_path
        self.tools_path = tools_path
        self.resources_path = resources_path
        self.hashes = file_hashes.HashCache(study_dir)
        self.runs = job_service.load_project_runs(user_id, project_id)
        self._pipelines: dict[str, object | None] = {}
        self._resolved: dict[str, dict] = {}
        self._tools: dict[str, object | None] = {}
        # execution_id → record (ledger shape, plus "reconstructed"/"_prov_mtime")
        self.execs: dict[str, dict] = {}
        # project-relative output path → execution_id of its current producer
        self.producer: dict[str, str] = {}

    # ── catalog lookups ──────────────────────────────────────────────────────

    def pipeline(self, pipeline_id: str):
        if pipeline_id not in self._pipelines:
            try:
                self._pipelines[pipeline_id] = catalog_service.get_pipeline(
                    self.pipelines_path, pipeline_id, tools_path=self.tools_path
                )
            except HTTPException:
                self._pipelines[pipeline_id] = None
        return self._pipelines[pipeline_id]

    def resolved_steps(self, pipeline_id: str) -> dict:
        """step_id → {tool, name, inputs, outputs} with paths resolved, project-relative."""
        if pipeline_id in self._resolved:
            return self._resolved[pipeline_id]
        out: dict = {}
        p = self.pipeline(pipeline_id)
        if p is not None:
            step_outputs: dict[str, dict[str, str]] = {}
            for sd in p.steps:
                ins = {k: job_service._resolve_path(v, self.study_dir, step_outputs) for k, v in sd.inputs.items()}
                outs = {k: job_service._resolve_path(v, self.study_dir, step_outputs) for k, v in sd.outputs.items()}
                step_outputs[sd.id] = outs
                out[sd.id] = {
                    "tool": sd.tool,
                    "name": sd.name,
                    "inputs": {k: job_service._project_rel(self.study_dir, v) for k, v in ins.items()},
                    "outputs": {k: job_service._project_rel(self.study_dir, v) for k, v in outs.items()},
                }
        self._resolved[pipeline_id] = out
        return out

    def step_definition(self, pipeline_id: str, step_id: str, tool_id: str | None) -> tuple[dict | None, str | None]:
        """The step's current definition, and the pipeline it was taken from.

        Falls back to another pipeline's step running the same tool (same step id
        preferred) when the original pipeline has since been renamed or removed.
        """
        sdef = self.resolved_steps(pipeline_id).get(step_id)
        if sdef is not None or not tool_id:
            return sdef, pipeline_id if sdef else None
        fallback = None
        for summary in catalog_service.list_pipelines(self.pipelines_path):
            for sid, cand in self.resolved_steps(summary.id).items():
                if cand["tool"] != tool_id:
                    continue
                if sid == step_id:
                    return cand, summary.id
                fallback = fallback or (cand, summary.id)
        return fallback or (None, None)

    def tool(self, tool_id: str | None):
        if not tool_id:
            return None
        if tool_id not in self._tools:
            try:
                self._tools[tool_id] = catalog_service.load_tool_spec(self.tools_path, tool_id)
            except Exception:
                self._tools[tool_id] = None
        return self._tools[tool_id]

    # ── executions ──────────────────────────────────────────────────────────

    def _find_run_step(self, pipeline_id: str, step_id: str, finished: float | None):
        if finished is None:
            return None, None
        for run in self.runs:
            if run.pipeline_id != pipeline_id:
                continue
            for st in run.steps:
                if (
                    st.step_id == step_id
                    and st.status == "succeeded"
                    and st.finished_at
                    and abs(st.finished_at.timestamp() - finished) < _TOLERANCE
                ):
                    return run, st
        return None, None

    def _reconstruct(self, prov: Path, data: dict) -> dict:
        pipeline_id = data.get("pipeline_id") or ""
        step_id = data.get("step_id") or ""
        generated_at = data.get("generated_at")
        run, st = self._find_run_step(pipeline_id, step_id, _ts(generated_at))
        run_id = data.get("run_id") or (run.run_id if run else None)
        sdef = self.resolved_steps(pipeline_id).get(step_id)
        tool_id = data.get("tool_id") or (st.tool_id if st else None) or (sdef["tool"] if sdef else None)
        spec = self.tool(tool_id)
        sdef, def_pipeline = self.step_definition(pipeline_id, step_id, tool_id)

        inputs = {
            k: job_service._project_rel(self.study_dir, v)
            for k, v in (data.get("input_paths") or {}).items()
        } or (sdef["inputs"] if sdef else {})
        outputs = self._outputs_on_disk(prov, dict(sdef["outputs"]) if sdef else {})
        params = dict(data.get("params") or {})
        if spec is not None:
            try:
                params = spec.resolve_params(params)
            except ValueError:
                pass

        rec = {
            "execution_id": step_ledger.execution_id(run_id or f"unrecorded-{generated_at}", step_id),
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "step_id": step_id,
            "tool_id": tool_id,
            "tool_name": getattr(spec, "name", None),
            "image": data.get("container_image") or getattr(spec, "image", ""),
            "image_digest": st.image_digest if st else None,
            "backend": data.get("backend", ""),
            "execution_mode": data.get("execution_mode", ""),
            "submitted_at": data.get("submitted_at"),
            "finished_at": generated_at,
            "params": params,
            "command": spec.render_command(params) if spec is not None else None,
            "gpus": bool((getattr(spec, "resources", None) or {}).get("gpus")),
            "singularity_run_mode": getattr(spec, "singularity_run_mode", None),
            "inputs": inputs,
            "outputs": outputs,
            "mounts": {
                label: {"container_path": m.path_in_container, "mode": m.mode, "mount_type": m.mount_type}
                for label, m in (spec.mounts.items() if spec is not None else [])
                if label in inputs or label in outputs
            },
            "chunks": len(st.chunks) if st else 0,
            "manual_sources": list(st.manual_sources) if st else [],
            "reconstructed": True,
            "_prov_mtime": prov.stat().st_mtime,
            "_tool_image_now": getattr(spec, "image", None),
            "_definition_from": def_pipeline if def_pipeline != pipeline_id else None,
        }
        return rec

    def _outputs_on_disk(self, prov: Path, outputs: dict[str, str]) -> dict[str, str]:
        """The definition's output paths, corrected to where this step really wrote.

        Pipeline definitions change; a _provenance.json does not move. An output
        the definition places elsewhere is looked for by name in the provenance
        file's directory, and that directory is always an output: it is where
        the step wrote. An output that still can't be placed keeps a synthetic
        label with no mount (``output:<path>``).
        """
        prov_dir = prov.parent.relative_to(self.study_dir).as_posix()
        for label, rel in list(outputs.items()):
            if Path(rel).is_absolute() or (self.study_dir / rel).exists():
                continue
            moved = f"{prov_dir}/{PurePosixPath(rel).name}"
            if (self.study_dir / moved).exists():
                outputs[label] = moved
        if not any(_related(o, prov_dir) for o in outputs.values()):
            outputs[f"output:{prov_dir}"] = prov_dir
        return outputs

    def _claim(self, rel: str, exec_id: str) -> None:
        if not (self.study_dir / rel).exists():
            return
        cur = self.producer.get(rel)
        if cur is None or (_ts(self.execs[exec_id].get("finished_at")) or 0) >= (
            _ts(self.execs[cur].get("finished_at")) or 0
        ):
            self.producer[rel] = exec_id

    def collect_executions(self) -> None:
        ledger = step_ledger.load(self.study_dir)
        for rel, exec_id in ledger["outputs"].items():
            rec = ledger["executions"].get(exec_id)
            if rec is None:
                continue
            self.execs.setdefault(exec_id, {**rec, "reconstructed": False})
            self._claim(rel, exec_id)

        for prov in sorted(self.study_dir.rglob("_provenance.json")):
            if set(prov.relative_to(self.study_dir).parts) & _EXCLUDE_DIRS:
                continue
            try:
                data = json.loads(prov.read_text())
            except (OSError, ValueError):
                continue
            if data.get("run_id") and step_ledger.execution_id(data["run_id"], data.get("step_id", "")) in ledger["executions"]:
                continue
            rec = self._reconstruct(prov, data)
            known = self.execs.get(rec["execution_id"])
            if known is None:
                self.execs[rec["execution_id"]] = rec
            else:
                # Same step, another of its output directories.
                for label, rel in rec["outputs"].items():
                    if rel not in known["outputs"].values():
                        known["outputs"][label] = rel
            for rel in rec["outputs"].values():
                self._claim(rel, rec["execution_id"])

        live = set(self.producer.values())
        self.execs = {k: v for k, v in self.execs.items() if k in live}

    def producers_of(self, rel: str, exclude: str | None = None) -> list[str]:
        return sorted({e for o, e in self.producer.items() if e != exclude and _related(rel, o)})

    def current_digest(self, rel: str) -> DatasetDigest:
        files = self.hashes.hash_tree(rel) if not Path(rel).is_absolute() else []
        return DatasetDigest(
            path=rel, files=len(files), bytes=sum(f.size for f in files),
            digest=file_hashes.tree_digest(files, rel),
        )

    def describe(self, rec: dict) -> ExecutionOut:
        exec_id = rec["execution_id"]
        warnings: list[str] = []
        sources: list[InputSource] = []
        depends: set[str] = set()
        recorded_in = rec.get("input_digests") or {}
        for label, rel in (rec.get("inputs") or {}).items():
            producers = self.producers_of(rel, exclude=exec_id)
            manual = (
                manual_upload_store.find_manual_data(self.study_dir, [self.study_dir / rel])
                if not Path(rel).is_absolute() else {}
            )
            if producers:
                depends.update(producers)
                kind, ref = "step", producers[0]
            elif manual:
                kind, ref = "manual", None
            elif Path(rel).is_absolute():
                kind, ref = "external", None
            elif _top(rel) in ingest_log.INPUT_ROOTS:
                kind, ref = "input", _top(rel)
            else:
                kind, ref = "unknown", None
            sources.append(InputSource(label=label, path=rel, kind=kind, ref=ref))
            if manual:
                warnings.append(f"Input {label} ({rel}) includes manually uploaded data.")

            if Path(rel).is_absolute():
                continue
            if label in recorded_in:
                if self.current_digest(rel).digest != recorded_in[label].get("digest"):
                    warnings.append(f"Input {label} ({rel}) has changed since this step read it.")
            else:
                ref_ts = _ts(rec.get("finished_at"))
                if ref_ts is not None:
                    check = _check_input(label, str(self.study_dir / rel), ref_ts + _TOLERANCE)
                    if check.status == "modified":
                        warnings.append(
                            f"Input {label} ({rel}): {check.modified_count} file(s) changed after this step ran."
                        )
                    elif check.status == "missing":
                        warnings.append(f"Input {label} ({rel}) no longer exists.")

        recorded_out = rec.get("output_digests") or {}
        outputs: list[DatasetDigest] = []
        for label, rel in (rec.get("outputs") or {}).items():
            if self.producer.get(rel) != exec_id:
                continue  # overwritten (or removed) since
            now = self.current_digest(rel)
            outputs.append(now)
            if label in recorded_out:
                if now.digest != recorded_out[label].get("digest"):
                    warnings.append(f"Output {rel} has changed since this step wrote it.")
            elif rec.get("_prov_mtime"):
                later = [
                    f for f in file_hashes.list_files(self.study_dir, rel)
                    if f.stat().st_mtime > rec["_prov_mtime"] + _TOLERANCE
                ]
                if later:
                    warnings.append(f"Output {rel}: {len(later)} file(s) changed after this step wrote them.")

        if rec.get("manual_sources"):
            warnings.append("This step read manually uploaded intermediate data when it ran.")
        commands = None
        if rec.get("command") is not None and rec.get("image"):
            commands = replication.commands(rec)
        else:
            warnings.append("The tool definition is no longer available, so its command can't be shown.")
        unmounted = [rel for label, rel in (rec.get("outputs") or {}).items() if label.startswith("output:")]
        if unmounted and commands:
            warnings.append(
                f"Couldn't match {', '.join(unmounted)} to the tool's outputs; the command doesn't mount it."
            )
        if rec.get("_definition_from"):
            warnings.append(
                f"Pipeline {rec['pipeline_id']} no longer exists; its paths are taken from the same "
                f"step in {rec['_definition_from']}."
            )
        if rec.get("reconstructed"):
            now_image = rec.get("_tool_image_now")
            if now_image and now_image != rec.get("image"):
                warnings.append(
                    f"The tool now uses {now_image}; the command uses {rec['image']}, the image this "
                    "step actually ran, with today's command template."
                )

        sdef = self.resolved_steps(rec["pipeline_id"]).get(rec["step_id"]) or {}
        return ExecutionOut(
            execution_id=exec_id,
            run_id=rec.get("run_id"),
            pipeline_id=rec["pipeline_id"],
            step_id=rec["step_id"],
            step_name=sdef.get("name"),
            tool_id=rec.get("tool_id"),
            tool_name=rec.get("tool_name"),
            image=rec.get("image") or "",
            image_digest=rec.get("image_digest"),
            backend=rec.get("backend") or "",
            execution_mode=rec.get("execution_mode") or "",
            submitted_at=rec.get("submitted_at"),
            finished_at=rec.get("finished_at"),
            params=rec.get("params") or {},
            command=rec.get("command"),
            inputs=sources,
            outputs=outputs,
            depends_on=sorted(depends),
            chunks=rec.get("chunks") or 0,
            reconstructed=bool(rec.get("reconstructed")),
            warnings=warnings,
            commands=commands,
        )

    # ── inputs ──────────────────────────────────────────────────────────────

    def inputs_and_events(self) -> tuple[list[InputDataset], list[IngestEventOut]]:
        ingest_log.begin(self.study_dir)  # seed the baseline / absorb untracked changes
        log = ingest_log.load(self.study_dir)
        kinds = {e["event_id"]: e["kind"] for e in log["events"]}
        tracked = log["files"]

        events: list[IngestEventOut] = []
        for e in log["events"]:
            touched = e.get("added", []) + e.get("changed", []) + e.get("removed", [])
            image = None
            if e["kind"] == "dicom_conversion" and e.get("detail", {}).get("run_id"):
                run = job_service._load_run(e["detail"]["run_id"])
                if run and run.steps:
                    image = run.steps[0].container_image
            mtimes = sorted(
                tracked[p]["mtime"] for p in e.get("added", [])
                if p in tracked and tracked[p].get("mtime")
            ) if e["kind"] == "baseline" else []
            events.append(IngestEventOut(
                event_id=e["event_id"],
                kind=e["kind"],
                at=e["at"],
                roots=sorted({_top(p) for p in touched}),
                added=len(e.get("added", [])),
                changed=len(e.get("changed", [])),
                removed=len(e.get("removed", [])),
                files=(e.get("added", []) + e.get("changed", []))[:_EVENT_FILES_SHOWN],
                detail=e.get("detail") or {},
                image=image,
                earliest_mtime=mtimes[0] if mtimes else None,
                latest_mtime=mtimes[-1] if mtimes else None,
            ))

        datasets: list[InputDataset] = []
        for root in ingest_log.INPUT_ROOTS:
            files = self.hashes.hash_tree(root)
            if not files:
                continue
            untracked = sum(
                1 for f in files
                if kinds.get(tracked.get(f.path, {}).get("event_id")) in (None, "baseline", "untracked_change")
            )
            touching = sorted((ev for ev in events if root in ev.roots), key=lambda ev: ev.at)
            times = [ev.at for ev in touching]
            first = times[0] if times else None
            from_mtime = bool(touching) and touching[0].kind == "baseline"
            if from_mtime:
                # The baseline is dated when tracking began; the files are older.
                first = min(
                    (tracked[f.path]["mtime"] for f in files if tracked.get(f.path, {}).get("mtime")),
                    default=first,
                )
            datasets.append(InputDataset(
                path=root,
                label=ingest_log.INPUT_ROOT_LABELS.get(root, root),
                files=len(files),
                bytes=sum(f.size for f in files),
                digest=file_hashes.tree_digest(files, root),
                untracked_files=untracked,
                first_recorded_at=first,
                first_recorded_from_mtime=from_mtime,
                last_changed_at=times[-1] if times else None,
            ))
        return datasets, events

    # ── results ─────────────────────────────────────────────────────────────

    def result_paths(self, pipeline_id: str) -> list[str]:
        spec = catalog_service.get_pipeline_results_spec(self.pipelines_path, pipeline_id)
        if spec is None:
            return []
        paths: list[str] = []
        if spec.batch_features and (self.study_dir / spec.batch_features.file).exists():
            paths.append(spec.batch_features.file)
        for ps in spec.per_subject:
            prefix = ps.pattern.split("{MRID}", 1)[0]
            d = prefix.rsplit("/", 1)[0] if "/" in prefix else prefix
            if d and (self.study_dir / d).exists() and d not in paths:
                paths.append(d)
        return paths


def _ordered(execs: list[ExecutionOut]) -> list[ExecutionOut]:
    """Dependencies first; otherwise by finish time."""
    by_id = {e.execution_id: e for e in execs}
    done: set[str] = set()
    out: list[ExecutionOut] = []

    def visit(e: ExecutionOut, stack: set[str]) -> None:
        if e.execution_id in done or e.execution_id in stack:
            return
        stack.add(e.execution_id)
        for dep in sorted(e.depends_on, key=lambda d: by_id[d].finished_at or "" if d in by_id else ""):
            if dep in by_id:
                visit(by_id[dep], stack)
        done.add(e.execution_id)
        out.append(e)

    for e in sorted(execs, key=lambda x: x.finished_at or ""):
        visit(e, set())
    return out


def build_dossier(
    study_dir: Path,
    project_id: str,
    user_id: str,
    pipelines_path: Path,
    tools_path: Path,
    resources_path: Path,
) -> ProvenanceDossier:
    b = _Builder(study_dir, project_id, user_id, pipelines_path, tools_path, resources_path)
    datasets, events = b.inputs_and_events()
    b.collect_executions()
    executions = _ordered([b.describe(rec) for rec in b.execs.values()])
    by_id = {e.execution_id: e for e in executions}

    results: list[ResultLineage] = []
    for summary in results_service.list_pipeline_results(study_dir, resources_path, pipelines_path):
        if not summary.has_outputs:
            continue
        paths = b.result_paths(summary.pipeline_id)
        warnings: list[str] = []
        roots: set[str] = set()
        chain: set[str] = set()
        for p in paths:
            producers = b.producers_of(p)
            if not producers:
                warnings.append(
                    f"No step record for {p}: produced before provenance tracking, or placed by hand."
                )
            stack = list(producers)
            while stack:
                eid = stack.pop()
                if eid in chain or eid not in by_id:
                    continue
                chain.add(eid)
                stack.extend(by_id[eid].depends_on)
        ordered = [e.execution_id for e in executions if e.execution_id in chain]
        for eid in ordered:
            ex = by_id[eid]
            ex.results.append(summary.pipeline_id)
            roots.update(s.ref for s in ex.inputs if s.kind == "input" and s.ref)
            if any(s.kind == "manual" for s in ex.inputs):
                warnings.append(f"Built on manually uploaded data (step {ex.step_name or ex.step_id}).")
        flagged = sum(1 for eid in ordered if by_id[eid].warnings)
        if flagged:
            warnings.append(
                f"{flagged} of its steps {'has' if flagged == 1 else 'have'} warnings."
                if len(ordered) > 1 else "Its step has warnings."
            )
        results.append(ResultLineage(
            pipeline_id=summary.pipeline_id,
            pipeline_name=summary.pipeline_name,
            paths=paths,
            path_notes={
                p: n for p, n in results_service.earlier_step_notes(pipelines_path, summary.pipeline_id).items()
                if p in paths
            },
            executions=ordered,
            inputs=sorted(roots),
            warnings=list(dict.fromkeys(warnings)),
        ))
    b.hashes.save()

    warnings: list[str] = []
    untracked = sum(d.untracked_files for d in datasets)
    if untracked:
        warnings.append(
            f"{untracked} input file(s) have no upload record: they were added before upload "
            "tracking began (or changed outside an upload). Their dates are file modification times."
        )
    reconstructed = sum(1 for e in executions if e.reconstructed)
    if reconstructed:
        warnings.append(
            f"{reconstructed} step(s) ran before full provenance tracking: their commands are rebuilt "
            "from the current tool definitions, and there are no checksums from when they ran."
        )
    flagged = sum(1 for e in executions if e.warnings)
    if flagged:
        warnings.append(
            f"{flagged} step{' has' if flagged == 1 else 's have'} warnings — see each step."
        )
    if any(r.warnings and any(w.startswith("No step record") for w in r.warnings) for r in results):
        warnings.append("Some results have no record of the step that produced them.")

    images = list(dict.fromkeys((e.image, e.image_digest) for e in executions if e.commands))
    notes = list(NOTES)
    if any(ev.kind == "dicom_conversion" for ev in events):
        notes.append(
            "Images converted from DICOM are treated as inputs: the DICOM files aren't kept, so the "
            "conversion itself isn't repeated. Its converter image is listed with the upload."
        )

    return ProvenanceDossier(
        project_id=project_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        api_version=job_service._API_VERSION,
        status="empty" if not results and not executions else ("warnings" if warnings else "ok"),
        warnings=warnings,
        inputs=datasets,
        events=events,
        executions=executions,
        results=results,
        setup=replication.setup_commands(images),
        notes=notes,
    )


def checksums(
    study_dir: Path,
    project_id: str,
    user_id: str,
    pipelines_path: Path,
    tools_path: Path,
    resources_path: Path,
) -> str:
    """``sha256sum``-format lines for every input file and every current step output."""
    b = _Builder(study_dir, project_id, user_id, pipelines_path, tools_path, resources_path)
    b.collect_executions()
    roots = list(ingest_log.INPUT_ROOTS) + sorted(
        rel for rel in b.producer if not Path(rel).is_absolute()
    )
    seen: dict[str, str] = {}
    for root in roots:
        for f in b.hashes.hash_tree(root):
            seen[f.path] = f.sha256
    b.hashes.save()
    return "".join(f"{sha}  {path}\n" for path, sha in sorted(seen.items()))
