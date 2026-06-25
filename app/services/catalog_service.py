"""
Catalog service — loads tool and pipeline YAML definitions from the resources directory.
"""

import csv
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException

from app.backends.base import MountSpec, ToolSpec
from app.models.catalog import (
    ColumnSpec,
    FeatureGroup,
    IOField,
    LabelInfo,
    ParameterSpec,
    PipelineDetail,
    PipelineStep,
    PipelineSummary,
    ResourceSpec,
    ToolDetail,
    ToolSummary,
)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return yaml.safe_load(f)


def _load_disabled_pipelines(pipelines_path: Path) -> set[str]:
    """Return pipeline IDs listed in disabled.txt (supports # comments)."""
    disabled_file = pipelines_path / "disabled.txt"
    if not disabled_file.exists():
        return set()
    disabled: set[str] = set()
    for raw_line in disabled_file.read_text().splitlines():
        line = raw_line.split("#")[0].strip()
        if line:
            disabled.add(line)
    return disabled


def get_tool(tools_path: Path, tool_id: str) -> ToolDetail:
    yaml_path = tools_path / f"{tool_id}.yaml"
    if not yaml_path.exists():
        raise HTTPException(404, f"Tool '{tool_id}' not found")
    data = _load_yaml(yaml_path)
    return ToolDetail(
        id=tool_id,
        name=data["name"],
        description=data.get("description"),
        inputs={k: IOField(**v) for k, v in (data.get("inputs") or {}).items()},
        outputs={k: IOField(**v) for k, v in (data.get("outputs") or {}).items()},
        resources=ResourceSpec(**data["resources"]),
        parameters={k: ParameterSpec(**v) for k, v in (data.get("parameters") or {}).items()},
        time_per_subject_seconds=data.get("time_per_subject_seconds"),
    )


def list_tools(tools_path: Path) -> list[ToolSummary]:
    result = []
    for yaml_path in sorted(tools_path.glob("*.yaml")):
        try:
            data = _load_yaml(yaml_path)
            result.append(ToolSummary(
                id=yaml_path.stem,
                name=data["name"],
                description=data.get("description"),
            ))
        except Exception:
            continue
    return result


def _parse_requires(raw: list | None) -> list[str]:
    result = []
    for item in raw or []:
        result.append(item if isinstance(item, str) else str(item))
    return result


def _parse_column_schemas(raw_requires: list | None) -> dict[str, ColumnSpec]:
    """Extract ColumnSpec entries from the csv_has_columns requirement block."""
    for item in raw_requires or []:
        if not isinstance(item, dict):
            continue
        cols = item.get("csv_has_columns")
        if not cols:
            continue
        schemas: dict[str, ColumnSpec] = {}
        for col in cols:
            if isinstance(col, str):
                schemas[col] = ColumnSpec(name=col)
            elif isinstance(col, dict) and col.get("name"):
                schemas[col["name"]] = ColumnSpec(
                    name=col["name"],
                    type=col.get("type", "string"),
                    min=col.get("min"),
                    max=col.get("max"),
                    values=[str(v) for v in col["values"]] if col.get("values") else None,
                    description=col.get("description"),
                )
        return schemas
    return {}


def _build_catalog_features(
    resources_path: Path,
    batch_spec: "BatchFeaturesSpec",
) -> tuple["dict[str, LabelInfo] | None", "list[FeatureGroup] | None"]:
    """Build label_map and feature_groups for the catalog endpoint.

    Unlike the results-side loader, this reads every entry from the label_map CSV
    without filtering by actual output columns (no project data available here).
    feature_groups are derived from compact label_id_range declarations in the YAML.
    """
    if not batch_spec.label_map:
        return None, None

    mapping_file = resources_path / batch_spec.label_map
    if not mapping_file.exists():
        return None, None

    label_map: dict[str, LabelInfo] = {}
    primary_ids: dict[str, int] = {}  # col_name -> primary label_id (for range grouping)

    try:
        with mapping_file.open() as f:
            for row in csv.reader(f):
                if len(row) < 2:
                    continue
                try:
                    label_id = int(row[0].strip())
                    display_name = row[1].strip()
                except (ValueError, IndexError):
                    continue
                constituents = [int(c.strip()) for c in row[2:] if c.strip()]
                if not constituents:
                    constituents = [label_id]
                col_name = batch_spec.column_template.replace("{id}", str(label_id))
                label_map[col_name] = LabelInfo(display_name=display_name, label_ids=constituents)
                primary_ids[col_name] = label_id
    except Exception:
        return None, None

    if not label_map:
        return None, None

    feature_groups: list[FeatureGroup] | None = None
    if batch_spec.feature_groups:
        feature_groups = []
        assigned: set[str] = set()
        for grp in batch_spec.feature_groups:
            name = grp["name"]
            id_range = grp.get("label_id_range")
            if id_range is None:
                # catch-all: every column not yet placed in a group
                columns = sorted(c for c in label_map if c not in assigned)
            else:
                lo, hi = id_range
                columns = sorted(
                    c for c in label_map
                    if lo <= primary_ids.get(c, -1) <= hi and c not in assigned
                )
            if columns:
                feature_groups.append(FeatureGroup(name=name, columns=columns))
                assigned.update(columns)

    return label_map, feature_groups


def get_pipeline(
    pipelines_path: Path,
    pipeline_id: str,
    resources_path: Path | None = None,
) -> PipelineDetail:
    if pipeline_id in _load_disabled_pipelines(pipelines_path):
        raise HTTPException(404, f"Pipeline '{pipeline_id}' not found")
    yaml_path = pipelines_path / f"{pipeline_id}.yaml"
    if not yaml_path.exists():
        raise HTTPException(404, f"Pipeline '{pipeline_id}' not found")
    data = _load_yaml(yaml_path)
    results_raw = data.get("results") or {}

    def _res(rel: str | None) -> str | None:
        if not rel or resources_path is None:
            return rel
        return rel if (resources_path / rel).exists() else None

    label_map = None
    feature_groups = None
    bf_raw = results_raw.get("batch_features")
    if bf_raw and resources_path:
        batch_spec = BatchFeaturesSpec(
            file=bf_raw["file"],
            mrid_column=bf_raw.get("mrid_column", "MRID"),
            label_map=bf_raw.get("label_map"),
            column_template=bf_raw.get("column_template", "{id}"),
            feature_groups=bf_raw.get("feature_groups"),
        )
        label_map, feature_groups = _build_catalog_features(resources_path, batch_spec)

    raw_requires = data.get("requires")
    return PipelineDetail(
        id=pipeline_id,
        name=data["pipeline_name"],
        description=data.get("description"),
        categories=data.get("categories") or [],
        requires=_parse_requires(raw_requires),
        steps=[
            PipelineStep(
                id=s["id"],
                tool=s["tool"],
                inputs=s.get("inputs") or {},
                outputs=s.get("outputs") or {},
                params=s.get("params") or {},
            )
            for s in (data.get("steps") or [])
        ],
        parameters={
            k: ParameterSpec(**v)
            for k, v in (data.get("parameters") or {}).items()
        },
        atlas_resource_path=_res(results_raw.get("atlas")),
        atlas_segmentation_resource_path=_res(results_raw.get("atlas_segmentation")),
        label_map=label_map,
        feature_groups=feature_groups,
        column_schemas=_parse_column_schemas(raw_requires),
    )


def load_tool_spec(tools_path: Path, tool_id: str) -> ToolSpec:
    """Load a tool YAML into the internal ToolSpec used by job backends."""
    yaml_path = tools_path / f"{tool_id}.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"Tool '{tool_id}' not found at {yaml_path}")
    data = _load_yaml(yaml_path)
    return ToolSpec(
        tool_id=tool_id,
        name=data["name"],
        image=data["container"]["image"],
        command_template=data["container"]["command"],
        mounts={k: MountSpec(**v) for k, v in (data.get("mounts") or {}).items()},
        parameters=data.get("parameters") or {},
        resources=data.get("resources") or {},
        time_per_subject_seconds=data.get("time_per_subject_seconds"),
        singularity_run_mode=data.get("container", {}).get("singularity_run_mode"),
    )


def get_pipeline_raw_requires(pipelines_path: Path, pipeline_id: str) -> list:
    """Return the raw ``requires`` list from a pipeline YAML.

    Each element is either a plain string (e.g. ``"needs_T1"``) or a dict
    (e.g. ``{"csv_has_columns": ["MRID", "Age"]}``). Returns an empty list
    when the pipeline has no requirements.
    """
    if pipeline_id in _load_disabled_pipelines(pipelines_path):
        raise HTTPException(404, f"Pipeline '{pipeline_id}' not found")
    yaml_path = pipelines_path / f"{pipeline_id}.yaml"
    if not yaml_path.exists():
        raise HTTPException(404, f"Pipeline '{pipeline_id}' not found")
    data = _load_yaml(yaml_path)
    return data.get("requires") or []


@dataclass
class BatchFeaturesSpec:
    file: str
    mrid_column: str = "MRID"
    label_map: str | None = None
    column_template: str = "{id}"
    feature_groups: list[dict] | None = None  # [{name, label_id_range: [lo, hi]}]


@dataclass
class PerSubjectSpec:
    id: str
    pattern: str
    type: str = "nifti"


@dataclass
class PipelineResultsSpec:
    batch_features: BatchFeaturesSpec | None = None
    per_subject: list[PerSubjectSpec] = dc_field(default_factory=list)
    atlas: str | None = None
    atlas_segmentation: str | None = None


def get_pipeline_results_spec(pipelines_path: Path, pipeline_id: str) -> "PipelineResultsSpec | None":
    """Return the parsed ``results:`` section of a pipeline YAML, or None if absent."""
    yaml_path = pipelines_path / f"{pipeline_id}.yaml"
    if not yaml_path.exists():
        raise HTTPException(404, f"Pipeline '{pipeline_id}' not found")
    data = _load_yaml(yaml_path)
    raw = data.get("results")
    if not raw:
        return None

    bf_raw = raw.get("batch_features")
    batch_features = None
    if bf_raw:
        batch_features = BatchFeaturesSpec(
            file=bf_raw["file"],
            mrid_column=bf_raw.get("mrid_column", "MRID"),
            label_map=bf_raw.get("label_map"),
            column_template=bf_raw.get("column_template", "{id}"),
            feature_groups=bf_raw.get("feature_groups"),
        )

    per_subject = [
        PerSubjectSpec(
            id=s["id"],
            pattern=s["pattern"],
            type=s.get("type", "nifti"),
        )
        for s in (raw.get("per_subject") or [])
    ]

    return PipelineResultsSpec(
        batch_features=batch_features,
        per_subject=per_subject,
        atlas=raw.get("atlas"),
        atlas_segmentation=raw.get("atlas_segmentation"),
    )


def list_pipelines(pipelines_path: Path) -> list[PipelineSummary]:
    disabled = _load_disabled_pipelines(pipelines_path)
    result = []
    for yaml_path in sorted(pipelines_path.glob("*.yaml")):
        if yaml_path.stem in disabled:
            continue
        try:
            data = _load_yaml(yaml_path)
            result.append(PipelineSummary(
                id=yaml_path.stem,
                name=data["pipeline_name"],
                description=data.get("description"),
                categories=data.get("categories") or [],
                requires=_parse_requires(data.get("requires")),
            ))
        except Exception:
            continue
    return result
