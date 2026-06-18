"""
Cloud service status endpoint.

Public (no auth required). Returns the current AWS Batch queue depth and an
estimated queue-drain time so clients can surface busyness to users.

IAM requirements (cloud mode)
------------------------------
The API server's role needs:
  - batch:ListJobs   on the cbica-nichart-jobqueue-standard queue
  - batch:DescribeJobs (no resource restriction)
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import public
from app.config import Settings, get_settings
from app.models.cloud import CloudStatusResponse
from app.models.errors import ErrorDetail

router = APIRouter(tags=["Cloud"])


@router.get(
    "/cloud/status",
    summary="Cloud queue status",
    description=(
        "Returns the number of running and pending jobs on the AWS Batch queue "
        "and a rough estimate of how long until the queue drains. "
        "In local mode all job-count fields are null. "
        "No authentication required."
    ),
    dependencies=[Depends(public)],
    response_model=CloudStatusResponse,
    responses={
        503: {"model": ErrorDetail, "description": "Could not reach the AWS Batch API."},
    },
)
async def cloud_status(
    settings: Settings = Depends(get_settings),
) -> CloudStatusResponse:
    if settings.execution_mode == "local":
        return CloudStatusResponse(mode="local")

    import boto3

    batch = boto3.client("batch", region_name=settings.cognito_region)

    try:
        running_jobs: list[dict] = (
            await asyncio.to_thread(
                batch.list_jobs,
                jobQueue=settings.batch_queue_name,
                jobStatus="RUNNING",
            )
        ).get("jobSummaryList", [])

        pending_jobs: list[dict] = []
        for status in ("SUBMITTED", "PENDING", "RUNNABLE"):
            resp = await asyncio.to_thread(
                batch.list_jobs,
                jobQueue=settings.batch_queue_name,
                jobStatus=status,
            )
            pending_jobs.extend(resp.get("jobSummaryList", []))

    except Exception as e:
        raise HTTPException(503, f"Could not reach AWS Batch: {e}")

    # Estimate queue-drain time using per-job tool metadata
    all_jobs = running_jobs + pending_jobs
    estimate: float | None = None

    if all_jobs:
        job_ids = [j["jobId"] for j in all_jobs[:100]]
        try:
            details = (
                await asyncio.to_thread(batch.describe_jobs, jobs=job_ids)
            ).get("jobs", [])

            from app.services.catalog_service import get_tool

            total_secs = 0.0
            has_estimate = False
            for job in details:
                params = job.get("parameters", {})
                tool_id = params.get("tool_id")
                try:
                    ns = int(params.get("num_subjects", 1))
                except (ValueError, TypeError):
                    ns = 1
                if tool_id:
                    try:
                        tool_detail = get_tool(settings.tools_path, tool_id)
                        if tool_detail.time_per_subject_seconds is not None:
                            total_secs += tool_detail.time_per_subject_seconds * ns
                            has_estimate = True
                    except Exception:
                        pass

            if has_estimate:
                estimate = total_secs
        except Exception:
            pass

    return CloudStatusResponse(
        mode="cloud",
        queue_name=settings.batch_queue_name,
        running_job_count=len(running_jobs),
        pending_job_count=len(pending_jobs),
        estimated_queue_drain_seconds=estimate,
    )
