"""
Catalog service — loads tool and pipeline YAML definitions from the resources directory.
"""

from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException

from app.backends.base import MountSpec, ToolSpec
from app.models.catalog import (
    IOField,
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


def get_pipeline(pipelines_path: Path, pipeline_id: str) -> PipelineDetail:
    yaml_path = pipelines_path / f"{pipeline_id}.yaml"
    if not yaml_path.exists():
        raise HTTPException(404, f"Pipeline '{pipeline_id}' not found")
    data = _load_yaml(yaml_path)
    return PipelineDetail(
        id=pipeline_id,
        name=data["pipeline_name"],
        description=data.get("description"),
        categories=data.get("categories") or [],
        requires=_parse_requires(data.get("requires")),
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
    result = []
    for yaml_path in sorted(pipelines_path.glob("*.yaml")):
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
