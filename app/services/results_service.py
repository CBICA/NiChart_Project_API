"""
Results service — inspects pipeline output files within a project and builds
the structured response used by the results endpoints.
"""

import csv
from pathlib import Path

from app.models.catalog import LabelInfo
from app.models.results import (
    BatchFeaturesResult,
    PerSubjectFileStatus,
    PerSubjectOutput,
    PipelineResultDetail,
    PipelineResultSummary,
    SubjectCompleteness,
)
from app.services.catalog_service import (
    BATCH_FEATURES_KEY,
    BatchFeaturesSpec,
    ResultOrigins,
    get_result_origins,
    PerSubjectSpec,
    PipelineResultsSpec,
    get_pipeline_results_spec,
    list_pipelines,
)
from app.services.file_service import read_participants


def _collect_mrids(project_path: Path, batch_spec: BatchFeaturesSpec | None) -> list[str]:
    """Return sorted union of MRIDs from participants.csv and the batch features CSV."""
    mrids: set[str] = set()

    try:
        participants = read_participants(project_path)
        mrids.update(r.MRID for r in participants.rows)
    except Exception:
        pass

    if batch_spec:
        features_path = project_path / batch_spec.file
        if features_path.exists():
            try:
                with features_path.open() as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        mrid = row.get(batch_spec.mrid_column, "").strip()
                        if mrid:
                            mrids.add(mrid)
            except Exception:
                pass

    return sorted(mrids)


def _load_label_map(
    resources_path: Path,
    label_map_rel: str,
    column_template: str,
    csv_columns: set[str],
    label_output_id: str | None = None,
) -> dict[str, LabelInfo] | None:
    """
    Parse a muse_mapping_derived-style CSV into {column_name -> LabelInfo}.

    CSV format (no header): label_id, display_name, constituent_id1, constituent_id2, ...
    Trailing empty cells (from variable-length rows) are ignored.
    The column name is column_template with ``{id}`` -> label_id and ``{name}`` ->
    display_name (the latter for atlases whose CSV columns are ROI names, e.g. SynthSeg).
    Only entries whose constructed column name exists in csv_columns are included.
    """
    mapping_file = resources_path / label_map_rel
    if not mapping_file.exists():
        return None

    result: dict[str, LabelInfo] = {}
    try:
        with mapping_file.open() as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 2:
                    continue
                try:
                    label_id = int(row[0].strip())
                    display_name = row[1].strip()
                except (ValueError, IndexError):
                    continue

                constituents = [
                    int(c.strip()) for c in row[2:] if c.strip()
                ]
                if not constituents:
                    constituents = [label_id]

                col_name = column_template.replace("{id}", str(label_id)).replace("{name}", display_name)
                if col_name in csv_columns:
                    result[col_name] = LabelInfo(
                        display_name=display_name,
                        label_ids=constituents,
                        output_id=label_output_id,
                    )
    except Exception:
        return None

    return result or None


def _build_batch_features(
    project_path: Path,
    resources_path: Path,
    spec: BatchFeaturesSpec,
) -> BatchFeaturesResult:
    features_path = project_path / spec.file
    if not features_path.is_file():
        return BatchFeaturesResult(available=False, display_name=spec.display_name)

    try:
        with features_path.open() as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        all_cols = list(rows[0].keys()) if rows else []
        feature_cols = [c for c in all_cols if c != spec.mrid_column]
        col_set = set(all_cols)

        label_map = None
        if spec.label_map:
            label_map = _load_label_map(
                resources_path, spec.label_map, spec.column_template, col_set,
                label_output_id=spec.label_output_id,
            )
            # Stamp units onto label_map entries if declared in the pipeline YAML.
            # Carry every other field across: rebuilding the entry here is how
            # output_id was once silently dropped for any pipeline that declares
            # units, which is most of them.
            if label_map and (spec.column_units or spec.default_unit):
                label_map = {
                    col: info.model_copy(update={
                        "unit": (spec.column_units or {}).get(col, spec.default_unit),
                    })
                    for col, info in label_map.items()
                }

        # Build flat column_units covering all feature columns.
        column_units: dict[str, str] | None = None
        if spec.column_units or spec.default_unit:
            units = {
                col: (spec.column_units or {}).get(col) or spec.default_unit
                for col in feature_cols
            }
            column_units = {col: u for col, u in units.items() if u} or None

        # Attach authored feature definitions, restricted to columns actually
        # present in this CSV (a definition for an absent column is ignored).
        feature_definitions = None
        if spec.features:
            present = {
                col: defn for col, defn in spec.features.items() if col in col_set
            }
            feature_definitions = present or None

        return BatchFeaturesResult(
            available=True,
            display_name=spec.display_name,
            download_path=spec.file,
            columns=feature_cols,
            row_count=len(rows),
            label_map=label_map,
            column_units=column_units,
            feature_definitions=feature_definitions,
        )
    except Exception:
        return BatchFeaturesResult(available=False, display_name=spec.display_name)


def _build_per_subject(
    project_path: Path,
    spec: PerSubjectSpec,
    mrids: list[str],
) -> PerSubjectOutput:
    subjects: dict[str, PerSubjectFileStatus] = {}
    for mrid in mrids:
        rel_path = spec.pattern.replace("{MRID}", mrid)
        available = (project_path / rel_path).exists()
        subjects[mrid] = PerSubjectFileStatus(
            available=available,
            download_path=rel_path if available else None,
        )
    return PerSubjectOutput(
        id=spec.id,
        type=spec.type,
        display_name=spec.display_name,
        underlay=spec.underlay,
        content=spec.content,
        subjects=subjects,
    )


def _own_results(origins: ResultOrigins | None) -> set[str] | None:
    """Result keys written by the pipeline's final result-producing step, or None
    when that can't be told (no steps declared)."""
    if origins is None or origins.final_step is None:
        return None
    return {k for k, step in origins.producers.items() if step == origins.final_step}


def _result_owners(pipelines_path: Path) -> dict[str, list[tuple[str, str, bool]]]:
    """Result location → (pipeline_id, name, harmonized) of every pipeline for which
    it is an own result (see ``_own_results``)."""
    owners: dict[str, list[tuple[str, str, bool]]] = {}
    for summary in list_pipelines(pipelines_path):
        origins = get_result_origins(pipelines_path, summary.id)
        for key in _own_results(origins) or ():
            owners.setdefault(origins.locations[key], []).append(
                (summary.id, summary.name, origins.harmonized)
            )
    return owners


def _earlier_origin(
    key: str,
    pipeline_id: str,
    origins: ResultOrigins,
    owners: dict[str, list[tuple[str, str, bool]]],
) -> tuple[str | None, str | None, str]:
    """For a result written by an earlier step: (same-as pipeline id, its name, note)."""
    candidates = [o for o in owners.get(origins.locations[key], []) if o[0] != pipeline_id]
    # Prefer the plain (non-harmonized) pipeline: that's what the file really is.
    candidates.sort(key=lambda o: o[2])
    if candidates:
        other_id, other_name, other_harmonized = candidates[0]
        caveat = " — not harmonized" if origins.harmonized and not other_harmonized else ""
        return other_id, other_name, f"From {other_name}{caveat}"
    return None, None, f"From an earlier step ({origins.producers.get(key)})"


def earlier_step_notes(pipelines_path: Path, pipeline_id: str) -> dict[str, str]:
    """Result location → note, for each result an earlier step writes (see ResultOrigins)."""
    origins = get_result_origins(pipelines_path, pipeline_id)
    own = _own_results(origins)
    if origins is None or own is None:
        return {}
    owners = _result_owners(pipelines_path)
    return {
        origins.locations[key]: _earlier_origin(key, pipeline_id, origins, owners)[2]
        for key in origins.producers if key not in own
    }


def _annotate_origin(
    output: PerSubjectOutput,
    pipeline_id: str,
    origins: ResultOrigins | None,
    owners: dict[str, list[tuple[str, str, bool]]],
) -> PerSubjectOutput:
    own = _own_results(origins)
    if origins is None or own is None:
        return output
    output.produced_by_step = origins.producers.get(output.id)
    if output.id in own:
        return output
    output.from_earlier_step = True
    (output.same_as_pipeline_id, output.same_as_pipeline_name,
     output.origin_note) = _earlier_origin(output.id, pipeline_id, origins, owners)
    return output


def _resource_path_if_exists(resources_path: Path, rel_path: str | None) -> str | None:
    if not rel_path:
        return None
    return rel_path if (resources_path / rel_path).exists() else None


def get_pipeline_result_detail(
    project_path: Path,
    resources_path: Path,
    pipelines_path: Path,
    pipeline_id: str,
    pipeline_name: str,
) -> PipelineResultDetail:
    spec = get_pipeline_results_spec(pipelines_path, pipeline_id)
    if spec is None:
        return PipelineResultDetail(
            pipeline_id=pipeline_id,
            pipeline_name=pipeline_name,
        )

    mrids = _collect_mrids(project_path, spec.batch_features)

    batch = (
        _build_batch_features(project_path, resources_path, spec.batch_features)
        if spec.batch_features
        else None
    )

    origins = get_result_origins(pipelines_path, pipeline_id)
    owners = _result_owners(pipelines_path) if _own_results(origins) is not None else {}
    per_subject = [
        _annotate_origin(_build_per_subject(project_path, ps_spec, mrids), pipeline_id, origins, owners)
        for ps_spec in spec.per_subject
    ]

    subjects: dict[str, SubjectCompleteness] = {
        mrid: SubjectCompleteness(
            complete=all(
                ps.subjects.get(mrid, PerSubjectFileStatus(available=False)).available
                for ps in per_subject
            ),
            missing=[
                ps.id
                for ps in per_subject
                if not ps.subjects.get(mrid, PerSubjectFileStatus(available=False)).available
            ],
        )
        for mrid in mrids
    }

    return PipelineResultDetail(
        pipeline_id=pipeline_id,
        pipeline_name=pipeline_name,
        batch_features=batch,
        per_subject=per_subject,
        atlas_resource_path=_resource_path_if_exists(resources_path, spec.atlas),
        atlas_segmentation_resource_path=_resource_path_if_exists(
            resources_path, spec.atlas_segmentation
        ),
        subjects=subjects,
    )


def list_pipeline_results(
    project_path: Path,
    resources_path: Path,
    pipelines_path: Path,
) -> list[PipelineResultSummary]:
    summaries = []
    # Subjects to probe per-subject output patterns with; the batch CSV's own MRIDs only
    # matter when that CSV exists, and then has_outputs is already true.
    subject_mrids = _collect_mrids(project_path, None)
    for pipeline_summary in list_pipelines(pipelines_path):
        spec = get_pipeline_results_spec(pipelines_path, pipeline_summary.id)
        if spec is None:
            continue

        has_batch = spec.batch_features is not None and (
            project_path / spec.batch_features.file
        ).is_file()

        def per_subject_present(ps) -> bool:
            return any(
                (project_path / ps.pattern.replace("{MRID}", mrid)).exists()
                for mrid in subject_mrids
            )

        # Only the pipeline's own results count: those of its final result-producing
        # step. A shared earlier output (the DLMUSE segmentation, under harmonized
        # DLMUSE) is no sign this pipeline ran. Without declared steps, any output counts.
        own = _own_results(get_result_origins(pipelines_path, pipeline_summary.id))
        if own is None:
            has_outputs = has_batch or any(per_subject_present(ps) for ps in spec.per_subject)
        else:
            has_outputs = (BATCH_FEATURES_KEY in own and has_batch) or any(
                per_subject_present(ps) for ps in spec.per_subject if ps.id in own
            )

        summaries.append(PipelineResultSummary(
            pipeline_id=pipeline_summary.id,
            pipeline_name=pipeline_summary.name,
            has_batch_features=has_batch,
            expected_batch_features_file=spec.batch_features.file if spec.batch_features else None,
            per_subject_ids=[ps.id for ps in spec.per_subject],
            has_atlas=_resource_path_if_exists(resources_path, spec.atlas) is not None,
            has_outputs=has_outputs,
        ))

    return summaries
