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


class ChunkStatus(BaseModel):
    """Status of one parallel subject chunk within a step."""

    chunk_idx: int = Field(description="Zero-based index of this chunk within the step.")
    status: Literal["pending", "running", "succeeded", "failed"] = Field(
        description="Execution state of this chunk."
    )
    subjects: list[str] = Field(
        default_factory=list,
        description="MRID stems assigned to this chunk.",
    )
    job_id: str | None = Field(
        default=None,
        description="Backend job ID for this chunk's container.",
    )
    submitted_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
    error: str | None = Field(default=None, description="Error message if this chunk failed.")


class StepStatus(BaseModel):
    """Status record for a single pipeline step."""

    step_id: str = Field(description="Step identifier within the pipeline definition.")
    tool_id: str = Field(description="Tool that this step invokes.")
    status: Literal["pending", "running", "succeeded", "failed", "skipped", "partially_failed"] = Field(
        description=(
            "Current execution state. 'partially_failed' means at least one chunk succeeded "
            "but at least one failed; the pipeline is halted and partial output is preserved."
        )
    )
    submitted_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
    job_id: str | None = Field(
        default=None,
        description=(
            "Backend job ID. For non-parallelized steps only; null when the step runs as chunks. "
            "See 'chunks' for per-chunk job IDs."
        ),
    )
    container_image: str | None = Field(
        default=None, description="Container image used for this step."
    )
    error: str | None = Field(default=None, description="Error message if the step failed.")
    cached_from_run_id: str | None = Field(
        default=None,
        description=(
            "Run ID that originally produced the cached output for this step. "
            "Only set when status is 'skipped' and the result was reused from a prior run."
        ),
    )
    # ── Parallel chunk progress ───────────────────────────────────────────────
    total_chunks: int | None = Field(
        default=None,
        description="Total number of parallel chunks for this step. Null for non-parallelized steps.",
    )
    completed_chunks: int | None = Field(
        default=None,
        description="Number of chunks that have succeeded so far.",
    )
    failed_chunks: int | None = Field(
        default=None,
        description="Number of chunks that have failed.",
    )
    total_subjects: int | None = Field(
        default=None,
        description="Total subjects assigned across all chunks.",
    )
    subjects_complete: int | None = Field(
        default=None,
        description="Subjects in all succeeded chunks (guaranteed to have output).",
    )
    chunks: list[ChunkStatus] = Field(
        default_factory=list,
        description="Per-chunk breakdown. Empty for non-parallelized steps.",
    )


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
    jobs_ahead: int | None = Field(
        default=None,
        description=(
            "Number of Batch jobs submitted before this one that are still waiting "
            "for compute capacity. Cloud mode only; null in local mode or when no "
            "step is currently pending on the Batch queue."
        ),
    )
    estimated_wait_seconds: float | None = Field(
        default=None,
        description=(
            "Estimated seconds until this job reaches the front of the queue, "
            "computed as the sum of (num_subjects × time_per_subject_seconds) for "
            "each job submitted ahead of this one. Null when timing data is "
            "unavailable for any ahead job."
        ),
    )


class PipelineRunLogs(BaseModel):
    """Aggregated logs for all completed steps in a pipeline run."""

    run_id: str
    logs: str = Field(description="Concatenated log output from all steps executed so far.")


class PipelineRunCreated(BaseModel):
    """Response after successfully submitting a pipeline run."""

    run_id: str = Field(description="Use this ID to poll /jobs/pipelines/{run_id} for status.")
    status: Literal["pending"] = Field(default="pending")
