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
