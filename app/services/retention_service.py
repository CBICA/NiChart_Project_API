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


def _mtime_expiry(
    project_dir: Path,
    project_id: str,
    user_id: str,
    retention_days: int,
) -> datetime | None:
    """Expiry derived from the project directory's mtime, for a project with no marker.

    The directory's mtime moves whenever an entry is added or removed, so it
    tracks real activity, and unlike the heartbeat it is not an object a user can
    reach and delete. That is the point: with no fallback, removing the marker
    makes a project unexpirable, so the cheapest way to keep data on the server
    forever is to delete the one object that measures its age.

    It is a weaker clock than the heartbeat — writing a file inside a
    subdirectory does not touch the project directory itself — but it only has to
    be good enough to stop a project living forever, and every path that should
    extend retention writes a heartbeat anyway.

    ``None`` if the directory cannot be stat'ed, which keeps an unreadable
    project from being condemned by a failure to measure it.
    """
    try:
        mtime = datetime.fromtimestamp(project_dir.stat().st_mtime, tz=timezone.utc)
    except OSError:
        _log.exception(
            "Project %r (user %r) has no retention heartbeat and its directory "
            "could not be stat'ed; treating its expiry as unknown.",
            project_id, user_id,
        )
        return None

    expiry = expires_at(mtime, retention_days)
    if expiry < datetime.now(timezone.utc):
        # The consequential case: this project is now a deletion candidate on the
        # strength of its mtime alone, so say so once per query rather than
        # letting it disappear with only the reaper's log to explain it.
        _log.warning(
            "Project %r (user %r) has no retention heartbeat; its directory mtime "
            "(%s) puts its expiry at %s, already past. It is a deletion candidate "
            "on the mtime fallback alone.",
            project_id, user_id, mtime.isoformat(), expiry.isoformat(),
        )
    else:
        _log.debug(
            "Project %r (user %r) has no retention heartbeat; using its directory "
            "mtime, which expires at %s.",
            project_id, user_id, expiry.isoformat(),
        )
    return expiry


async def resolve_expiry(
    bucket: str | None,
    prefix: str,
    user_id: str,
    project_id: str,
    project_dir: Path,
    retention_days: int,
    mtime_fallback: bool,
) -> datetime | None:
    """The project's effective expiry, or ``None`` when there is no clock to read.

    See ``resolve_retention``, which this wraps.
    """
    expiry, _ = await resolve_retention(
        bucket, prefix, user_id, project_id, project_dir, retention_days, mtime_fallback
    )
    return expiry


async def resolve_retention(
    bucket: str | None,
    prefix: str,
    user_id: str,
    project_id: str,
    project_dir: Path,
    retention_days: int,
    mtime_fallback: bool,
) -> tuple[datetime | None, datetime | None]:
    """The project's effective expiry (``None`` when there is no clock to read), and
    when its retention was last extended: the heartbeat's time, when the heartbeat is
    the clock (``None`` for a test override or the directory-mtime fallback).

    The heartbeat is written when the project is created and rewritten by an explicit
    refresh and by use (uploads, pipeline submission), so "last extended" covers all
    three; a client compares it with the creation time to tell them apart.

    The single definition of "when does this project expire", shared by the
    retention endpoints and the reaper so the value the API reports and the one
    it acts on cannot disagree.

    Clocks, in the order they are consulted: a local test override, the heartbeat
    object, and — with ``mtime_fallback`` — the project directory's mtime.

    ``None`` means *unknown*, and callers must never read it as *expired*:

    - local mode, or no data bucket — retention is not tracked at all;
    - the heartbeat object does not exist and ``mtime_fallback`` is off;
    - S3 could not be reached. Failing open keeps a network blip from deleting
      data; the next query re-reads the marker. The fallback deliberately does
      *not* apply here: an absent marker is a fact, an unreadable one is not, and
      a project whose heartbeat is perfectly current must not be condemned by an
      outage. Note that ``head_object`` answers 403 rather than 404 when the
      caller lacks bucket-level permission, so a misconfigured role lands here
      and keeps the old fail-open behaviour.
    """
    override = read_retention_override(project_dir)
    if override is not None:
        return override, None
    if not bucket:
        return None, None
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
        return None, None
    if last_modified is None:
        if not mtime_fallback:
            return None, None
        return _mtime_expiry(project_dir, project_id, user_id, retention_days), None
    return expires_at(last_modified, retention_days), last_modified


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
