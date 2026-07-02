"""
Project retention heartbeat — cloud mode only.

The retention clock is the S3 LastModified of a zero-byte marker object:
  s3://{bucket}/{prefix}/{user_id}/{project_id}/.retention_heartbeat

The Lambda sweep runs daily and deletes any project whose heartbeat
LastModified is older than PROJECT_RETENTION_DAYS. Writing (or overwriting)
the heartbeat resets the countdown to now + PROJECT_RETENTION_DAYS.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

_HEARTBEAT_FILENAME = ".retention_heartbeat"


def _heartbeat_key(prefix: str, user_id: str, project_id: str) -> str:
    return f"{prefix}/{user_id}/{project_id}/{_HEARTBEAT_FILENAME}"


def _write_heartbeat_sync(bucket: str, key: str) -> datetime:
    import boto3
    s3 = boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=key, Body=b"")
    return datetime.now(timezone.utc)


def _read_heartbeat_sync(bucket: str, key: str) -> datetime | None:
    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3")
    try:
        resp = s3.head_object(Bucket=bucket, Key=key)
        last_modified: datetime = resp["LastModified"]
        if last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=timezone.utc)
        return last_modified
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return None
        raise


async def write_heartbeat(bucket: str, prefix: str, user_id: str, project_id: str) -> datetime:
    """Put (or overwrite) the heartbeat marker. Returns the write timestamp (now)."""
    key = _heartbeat_key(prefix, user_id, project_id)
    return await asyncio.to_thread(_write_heartbeat_sync, bucket, key)


async def read_heartbeat(bucket: str, prefix: str, user_id: str, project_id: str) -> datetime | None:
    """Return the LastModified of the heartbeat marker, or None if it doesn't exist."""
    key = _heartbeat_key(prefix, user_id, project_id)
    return await asyncio.to_thread(_read_heartbeat_sync, bucket, key)


def expires_at(last_modified: datetime, retention_days: int) -> datetime:
    return last_modified + timedelta(days=retention_days)


# ── Local override (test endpoints only) ─────────────────────────────────────
# Written by POST /test/projects/expire-soon; read by GET /projects/{id}/retention
# before the S3 path. Lets the frontend be tested with a near-expiry project
# without needing to manipulate S3 timestamps.

_OVERRIDE_FILENAME = "_working/retention_expires_at_override"


def write_retention_override(project_dir: Path, expires_at_ts: datetime) -> None:
    p = project_dir / _OVERRIDE_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(expires_at_ts.isoformat())


def read_retention_override(project_dir: Path) -> datetime | None:
    p = project_dir / _OVERRIDE_FILENAME
    if not p.exists():
        return None
    try:
        return datetime.fromisoformat(p.read_text().strip())
    except Exception:
        return None


def clear_retention_override(project_dir: Path) -> None:
    p = project_dir / _OVERRIDE_FILENAME
    p.unlink(missing_ok=True)
