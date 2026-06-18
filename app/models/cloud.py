"""
Response schemas for the cloud status endpoint.
"""

from typing import Literal

from pydantic import BaseModel, Field


class CloudStatusResponse(BaseModel):
    """
    Current cloud service busyness as reported by the AWS Batch queue.

    In local mode all job-count fields are ``null`` and ``mode`` is ``"local"``.
    """

    mode: Literal["cloud", "local"] = Field(
        description="Active execution mode for this server instance."
    )
    queue_name: str | None = Field(
        default=None,
        description="Name of the AWS Batch job queue being monitored.",
    )
    running_job_count: int | None = Field(
        default=None,
        description="Number of jobs currently in RUNNING state on the Batch queue.",
    )
    pending_job_count: int | None = Field(
        default=None,
        description="Number of jobs currently in PENDING (queued, not yet running) state.",
    )
    estimated_queue_drain_seconds: float | None = Field(
        default=None,
        description=(
            "Rough estimate of seconds until the queue is empty, computed as the sum of "
            "(time_per_subject_seconds × num_subjects) across all running and pending jobs. "
            "Null when no per-job subject counts are available."
        ),
    )
