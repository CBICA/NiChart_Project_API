"""
Response schemas for the pipeline/tool catalog endpoints.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ParameterSpec(BaseModel):
    """Specification for a single configurable parameter."""

    type: str = Field(description="Python type name: 'int', 'float', 'bool', or 'str'.")
    default: Any | None = Field(default=None, description="Default value if not supplied by the caller.")
    description: str | None = Field(default=None, description="Human-readable description for UI rendering.")
    choices: list[Any] | None = Field(default=None, description="Exhaustive list of allowed values, if constrained.")
    min: float | None = Field(default=None, description="Minimum value (numeric types only).")
    max: float | None = Field(default=None, description="Maximum value (numeric types only).")


class IOField(BaseModel):
    """An input or output slot on a tool."""

    type: Literal["file", "directory"] = Field(description="Whether this slot is a single file or a directory.")
    description: str | None = Field(default=None, description="Human-readable description of this slot.")


class ResourceSpec(BaseModel):
    """Compute resources required by a tool."""

    vcpus: int = Field(description="Number of virtual CPUs.")
    memory: int = Field(description="Memory in MiB.")
    gpus: int = Field(default=0, description="Number of GPUs required.")


class ToolSummary(BaseModel):
    """Abbreviated tool information for list responses."""

    id: str = Field(description="Tool identifier (YAML basename without extension).")
    name: str = Field(description="Human-readable tool name.")
    description: str | None = Field(default=None)


class ToolDetail(ToolSummary):
    """Full tool specification."""

    inputs: dict[str, IOField] = Field(description="Named input slots.")
    outputs: dict[str, IOField] = Field(description="Named output slots.")
    resources: ResourceSpec
    parameters: dict[str, ParameterSpec] = Field(default_factory=dict)
    time_per_subject_seconds: float | None = Field(
        default=None,
        description=(
            "Expected wall-clock seconds to process one subject. "
            "Used by GET /cloud/status to estimate queue-drain time."
        ),
    )


class PipelineStep(BaseModel):
    """A single step within a pipeline definition."""

    id: str = Field(description="Step identifier, unique within the pipeline.")
    tool: str = Field(description="Tool ID this step invokes.")
    inputs: dict[str, str] = Field(description="Input slot → path template mapping.")
    outputs: dict[str, str] = Field(description="Output slot → path template mapping.")
    params: dict[str, Any] = Field(default_factory=dict, description="Parameter overrides for this step.")


class PipelineSummary(BaseModel):
    """Abbreviated pipeline information for list responses."""

    id: str = Field(description="Pipeline identifier (YAML basename without extension).")
    name: str = Field(description="Human-readable pipeline name.")
    description: str | None = Field(default=None)
    categories: list[str] = Field(default_factory=list)
    requires: list[str] = Field(
        default_factory=list,
        description="Data prerequisites (e.g. 'needs_T1', 'needs_demographics').",
    )
    is_harmonized: bool = Field(
        default=False,
        description="True if this pipeline applies harmonization to its inputs.",
    )
    harmonized_variant: str | None = Field(
        default=None,
        description=(
            "Pipeline ID of the harmonized version of this pipeline. "
            "Present on base pipelines only; null on harmonized pipelines and those "
            "with no harmonized counterpart. Use to render a 'Switch to harmonized' action."
        ),
    )
    base_variant: str | None = Field(
        default=None,
        description=(
            "Pipeline ID of the standard (non-harmonized) version of this pipeline. "
            "Present on harmonized pipelines only; null on base pipelines and those "
            "with no base counterpart. Use to render a 'Switch to standard' action."
        ),
    )


class ColumnSpec(BaseModel):
    """Validation schema for a single required participants.csv column."""

    name: str = Field(description="Column name as it must appear in the CSV header.")
    type: Literal["string", "int", "float", "categorical"] = Field(
        default="string",
        description=(
            "'string' — any non-empty text. "
            "'int' — whole number, optionally bounded by min/max. "
            "'float' — decimal number, optionally bounded by min/max. "
            "'categorical' — must be one of the strings listed in values."
        ),
    )
    min: float | None = Field(
        default=None,
        description="Inclusive lower bound for numeric types. Null means no lower bound.",
    )
    max: float | None = Field(
        default=None,
        description="Inclusive upper bound for numeric types. Null means no upper bound.",
    )
    values: list[str] | None = Field(
        default=None,
        description="Exhaustive list of accepted values for categorical columns.",
    )
    description: str | None = Field(
        default=None,
        description="Human-readable description shown in the UI alongside the column input.",
    )


class LabelInfo(BaseModel):
    """Display metadata for a single feature column.

    ``label_ids`` is only present for pipelines that produce a segmentation NIfTI.
    When present, the values are the voxel intensities in the atlas segmentation that
    together form this region and should be used to build ROI overlays.
    For pipelines without segmentation output, ``label_map`` on ``PipelineDetail``
    will be null rather than containing ``LabelInfo`` entries with empty label_ids.
    """

    display_name: str = Field(description="Human-readable region name.")
    label_ids: list[int] | None = Field(
        default=None,
        description=(
            "Voxel values in the segmentation NIfTI that together form this region. "
            "Null for pipelines that do not produce a segmentation output."
        ),
    )


class FeatureGroup(BaseModel):
    """A named group of feature columns for hierarchical display in the UI."""

    name: str = Field(description="Display name for this group (e.g. 'Lobar', 'Global').")
    columns: list[str] = Field(description="Feature column names belonging to this group.")


class PipelineDetail(PipelineSummary):
    """Full pipeline definition including ordered steps and user-configurable parameters."""

    steps: list[PipelineStep] = Field(default_factory=list)
    parameters: dict[str, ParameterSpec] = Field(
        default_factory=dict,
        description=(
            "User-overridable parameters for this pipeline. "
            "Each entry describes the type, default, and optional constraints. "
            "Pass values via ``params`` in the pipeline submit body. "
            "Step-level params in the YAML are fixed by the pipeline author and "
            "cannot be overridden."
        ),
    )
    atlas_resource_path: str | None = Field(
        default=None,
        description=(
            "Resource path for the brain atlas NIfTI declared by this pipeline. "
            "Fetch with GET /catalog/resources/{path}. "
            "Null if no atlas is declared or the file is not present on the server."
        ),
    )
    atlas_segmentation_resource_path: str | None = Field(
        default=None,
        description=(
            "Resource path for the atlas segmentation NIfTI declared by this pipeline. "
            "Fetch with GET /catalog/resources/{path}. "
            "Null if no atlas segmentation is declared or the file is not present."
        ),
    )
    label_map: dict[str, LabelInfo] | None = Field(
        default=None,
        description=(
            "Maps each batch-feature column name to its display name and, when the "
            "pipeline produces a segmentation, the constituent voxel label IDs in the "
            "atlas segmentation NIfTI. Null if no label_map resource is configured or "
            "the resource file is absent. Pipelines without segmentation output will "
            "have this field null."
        ),
    )
    feature_groups: list[FeatureGroup] | None = Field(
        default=None,
        description=(
            "Ordered grouping of batch-feature columns for hierarchical UI display "
            "(e.g. nested dropdowns). Null if the pipeline does not declare "
            "feature_groups in its YAML."
        ),
    )
    column_schemas: dict[str, ColumnSpec] = Field(
        default_factory=dict,
        description=(
            "Validation schema for each column declared in csv_has_columns. "
            "Keyed by column name. Use this to drive client-side CSV validation: "
            "type checking, numeric range enforcement, and categorical value lists. "
            "Columns not listed here have no declared schema (accept any non-empty value)."
        ),
    )
