"""
AWS Batch backend — submits tool jobs via the ``cbica-nichart-submitjob`` Lambda.

Security boundary
-----------------
The Lambda loads the tool spec from S3, validates all mount paths within
``/fsx/fsx/{user_id}/``, and generates the Docker command internally.  The API
server never sends ``image`` or ``command`` to the Lambda — only:

    id_token, tool_name, user_mounts, user_params, num_subjects

Cloud setup requirements
------------------------
IAM permissions required on the API server's execution role:

- ``lambda:InvokeFunction`` on ``cbica-nichart-submitjob``
- ``batch:DescribeJobs`` (no resource restriction)
- ``batch:ListJobs`` on the job queue ARN
- ``batch:TerminateJob`` (no resource restriction)
- ``logs:GetLogEvents`` on ``arn:aws:logs:us-east-1:*:log-group:/aws/batch/job:*``

``NICHART_DATA_ROOT`` must be set to the FSx mount root (e.g. ``/fsx/fsx``)
so resolved data paths match what the Lambda expects.
"""

import asyncio
import json
from typing import Any

import boto3

from app.backends.base import JobBackend, JobHandle, ToolSpec
from app.config import Settings

_BATCH_LOG_GROUP = "/aws/batch/job"


class BatchJobHandle(JobHandle):
    """Wraps an AWS Batch job ID."""

    _STATUS_MAP = {
        "SUBMITTED": "pending",
        "PENDING": "pending",
        "RUNNABLE": "pending",
        "STARTING": "running",
        "RUNNING": "running",
        "SUCCEEDED": "succeeded",
        "FAILED": "failed",
    }

    def __init__(self, job_id: str, batch_client, logs_client) -> None:
        self._job_id = job_id
        self._batch = batch_client
        self._logs = logs_client

    @property
    def job_id(self) -> str:
        return self._job_id

    async def status(self) -> str:
        resp = await asyncio.to_thread(self._batch.describe_jobs, jobs=[self._job_id])
        if not resp["jobs"]:
            return "failed"
        return self._STATUS_MAP.get(resp["jobs"][0]["status"], "running")

    async def logs(self, tail: int = 200) -> str:
        resp = await asyncio.to_thread(self._batch.describe_jobs, jobs=[self._job_id])
        if not resp["jobs"]:
            return f"Job {self._job_id} not found"
        job = resp["jobs"][0]

        log_stream: str | None = None
        try:
            containers = (
                job.get("ecsProperties", {})
                .get("taskProperties", [{}])[0]
                .get("containers", [{}])
            )
            if containers:
                log_stream = containers[0].get("logStreamName")
        except (KeyError, IndexError):
            pass

        if not log_stream:
            return f"[No log stream yet — job {self._job_id} status: {job['status']}]"

        try:
            cw_resp = await asyncio.to_thread(
                self._logs.get_log_events,
                logGroupName=_BATCH_LOG_GROUP,
                logStreamName=log_stream,
                startFromHead=False,
                limit=tail,
            )
            events = cw_resp.get("events", [])
            if not events:
                return f"[No log events yet — job {self._job_id} status: {job['status']}]"
            return "\n".join(e["message"] for e in events)
        except Exception as exc:
            return f"[CloudWatch error for job {self._job_id}: {exc}]"

    async def cancel(self) -> None:
        await asyncio.to_thread(
            self._batch.terminate_job,
            jobId=self._job_id,
            reason="Cancelled by user via NiChart API",
        )


class BatchBackend(JobBackend):
    """Submits tool jobs via the cbica-nichart-submitjob Lambda function."""

    def __init__(self, settings: Settings) -> None:
        region = settings.cognito_region
        self._lambda = boto3.client("lambda", region_name=region)
        self._batch = boto3.client("batch", region_name=region)
        self._logs = boto3.client("logs", region_name=region)
        self._lambda_name = settings.lambda_function_name

    async def submit(
        self,
        tool_spec: ToolSpec,
        mount_paths: dict[str, str],
        params: dict[str, Any],
        num_subjects: int = 1,
        user_token: str | None = None,
    ) -> BatchJobHandle:
        payload = {
            "id_token": user_token or "",
            "tool_name": tool_spec.tool_id,
            "user_mounts": mount_paths,
            "user_params": params,
            "num_subjects": num_subjects,
        }
        resp = await asyncio.to_thread(
            self._lambda.invoke,
            FunctionName=self._lambda_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        result = json.loads(resp["Payload"].read())
        if result.get("statusCode") != 200:
            body_str = result.get("body") or "{}"
            try:
                body = json.loads(body_str)
                msg = body.get("error", body_str)
            except Exception:
                msg = body_str
            raise RuntimeError(f"Lambda error ({result.get('statusCode')}): {msg}")
        body = json.loads(result["body"])
        return BatchJobHandle(body["job_id"], self._batch, self._logs)
