"""
Request/response schemas for the pipeline jobs endpoints.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class PipelineRunSubmit(BaseModel):
    """Request body to submit a pipeline run."""

    pipeline_id: str = Field(description="Pipeline identifier (matches a YAML basename in resources/pipelines/).")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameter overrides applied to every step that accepts them.",
    )
    reuse_cached_steps: bool = Field(
        default=True,
        description=(
            "When True, steps whose inputs haven't changed since the last successful run "
            "are skipped. Set to False to force a full re-run."
        ),
    )


class StepStatus(BaseModel):
    """Status record for a single pipeline step."""

    step_id: str = Field(description="Step identifier within the pipeline definition.")
    tool_id: str = Field(description="Tool that this step invokes.")
    status: Literal["pending", "running", "succeeded", "failed", "skipped"] = Field(
        description="Current execution state."
    )
    submitted_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
    job_id: str | None = Field(
        default=None, description="Backend job ID (Docker container name or AWS Batch job ID)."
    )
    error: str | None = Field(default=None, description="Error message if the step failed.")


class PipelineRunSummary(BaseModel):
    """Abbreviated pipeline run record for list responses."""

    run_id: str = Field(description="Unique run identifier (UUID).")
    project_id: str
    pipeline_id: str
    status: Literal["pending", "running", "succeeded", "failed"] = Field(
        description="Overall pipeline status."
    )
    submitted_at: datetime
    finished_at: datetime | None = Field(default=None)
    current_step: int = Field(default=0, description="Index of the step currently executing (0-based).")
    total_steps: int = Field(default=0)


class PipelineRunDetail(PipelineRunSummary):
    """Full pipeline run record including per-step breakdown."""

    steps: list[StepStatus] = Field(default_factory=list)
    error: str | None = Field(default=None, description="Top-level error message if the run failed.")


class PipelineRunLogs(BaseModel):
    """Aggregated logs for all completed steps in a pipeline run."""

    run_id: str
    logs: str = Field(description="Concatenated log output from all steps executed so far.")


class PipelineRunCreated(BaseModel):
    """Response after successfully submitting a pipeline run."""

    run_id: str = Field(description="Use this ID to poll /jobs/pipelines/{run_id} for status.")
    status: Literal["pending"] = Field(default="pending")
