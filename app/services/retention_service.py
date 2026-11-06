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
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

_HEARTBEAT_FILENAME = ".retention_heartbeat"

_client_lock = threading.Lock()
_s3_client = None


def _s3():
    """The shared S3 client used for heartbeat reads and writes.

    Listing a user's projects reads one heartbeat per project, concurrently in
    worker threads. botocore clients are thread-safe to *use*, but building one
    is not, so the client is created once under a lock and then reused rather
    than constructed per call inside each thread.
    """
    global _s3_client
    with _client_lock:
        if _s3_client is None:
            import boto3
            _s3_client = boto3.client("s3")
    return _s3_client


def _heartbeat_key(prefix: str, user_id: str, project_id: str) -> str:
    return f"{prefix}/{user_id}/{project_id}/{_HEARTBEAT_FILENAME}"


def _write_heartbeat_sync(bucket: str, key: str) -> datetime:
    _s3().put_object(Bucket=bucket, Key=key, Body=b"")
    return datetime.now(timezone.utc)


def _read_heartbeat_sync(bucket: str, key: str) -> datetime | None:
    from botocore.exceptions import ClientError
    try:
        resp = _s3().head_object(Bucket=bucket, Key=key)
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


async def touch_project(
    bucket: str | None,
    prefix: str,
    user_id: str,
    project_id: str,
    activity: str,
) -> None:
    """Reset a project's retention countdown because it is being used.

    The sweep Lambda measures staleness from the heartbeat object alone, not from
    the project's other objects — so work that rewrites hundreds of files under
    the prefix does not, by itself, keep a project alive. Running a pipeline is
    the strongest signal of use there is, and with a 7-day default window a
    project used every day could otherwise still be deleted.

    No-op when no S3 bucket is configured (local mode). Never raises: failing to
    refresh must not fail the action that triggered it, and the daily sweep
    backfills a missing heartbeat.
    """
    if not bucket:
        return
    try:
        await write_heartbeat(
            bucket=bucket, prefix=prefix, user_id=user_id, project_id=project_id
        )
        _log.debug(
            "Refreshed retention heartbeat for project %r (user %r) on %s",
            project_id, user_id, activity,
        )
    except Exception:
        _log.exception(
            "Failed to refresh retention heartbeat for project %r (user %r) on %s; "
            "Lambda will backfill on next sweep.",
            project_id, user_id, activity,
        )


def expires_at(last_modified: datetime, retention_days: int) -> datetime:
    return last_modified + timedelta(days=retention_days)


async def resolve_expiry(
    bucket: str | None,
    prefix: str,
    user_id: str,
    project_id: str,
    project_dir: Path,
    retention_days: int,
) -> datetime | None:
    """The project's effective expiry, or ``None`` when there is no clock to read.

    The single definition of "when does this project expire", shared by the
    retention endpoints and the reaper so the value the API reports and the one
    it acts on cannot disagree.

    ``None`` means *unknown*, and callers must never read it as *expired*:

    - local mode, or no data bucket — retention is not tracked at all;
    - the heartbeat object does not exist — a project created before the
      heartbeat shipped, or one whose marker was swept while its files remain.
      Deleting on that would destroy a project that was never given a clock;
    - S3 could not be reached. Failing open keeps a network blip from deleting
      data; the next query re-reads the marker.

    A local test override (see below) takes precedence over S3 so the expiry
    path can be exercised without manipulating object timestamps.
    """
    override = read_retention_override(project_dir)
    if override is not None:
        return override
    if not bucket:
        return None
    try:
        last_modified = await read_heartbeat(
            bucket=bucket, prefix=prefix, user_id=user_id, project_id=project_id
        )
    except Exception:
        _log.exception(
            "Could not read the retention heartbeat for project %r (user %r); "
            "treating its expiry as unknown.",
            project_id, user_id,
        )
        return None
    if last_modified is None:
        return None
    return expires_at(last_modified, retention_days)


def overdue_by(expiry: datetime | None, now: datetime | None = None) -> timedelta | None:
    """How long past ``expiry`` we are, or ``None`` if it is unknown or not yet due."""
    if expiry is None:
        return None
    delta = (now or datetime.now(timezone.utc)) - expiry
    return delta if delta > timedelta(0) else None


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
