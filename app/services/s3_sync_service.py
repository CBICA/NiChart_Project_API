"""
S3 sync helpers for cloud-mode pipeline orchestration.

Implements a size-based incremental sync (analogous to ``aws s3 sync``) using
boto3 directly — no CLI dependency required inside the container.

Usage pattern in a pipeline run:
1. Before submitting each step: ``sync_to_s3()`` — upload local project files
   so the Batch job can pull them via its own S3 sync at container start.
2. After each step completes (success or failure): ``sync_from_s3()`` — download
   outputs that the Batch job wrote to S3 so the API server can serve them.

The _upload/ staging directory is excluded from sync; it contains uncommitted
upload staging files that are not inputs or outputs of any pipeline step.
"""

import asyncio
import logging
from pathlib import Path

import boto3

_log = logging.getLogger(__name__)

# Directories under the project root excluded from all sync operations.
_EXCLUDE_TOP_DIRS = frozenset({"_upload"})


def _should_exclude(rel: str) -> bool:
    parts = Path(rel).parts
    return bool(parts and parts[0] in _EXCLUDE_TOP_DIRS)


def _upload_sync(local_dir: Path, bucket: str, prefix: str) -> int:
    """Upload new/changed local files to S3. Returns number of files uploaded."""
    s3 = boto3.client("s3")

    # Index existing S3 objects by relative key → size for fast size comparison.
    existing: dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix + "/"):
        for obj in page.get("Contents", []):
            rel_key = obj["Key"][len(prefix) + 1:]
            existing[rel_key] = obj["Size"]

    uploaded = 0
    for file_path in sorted(local_dir.rglob("*")):
        if not file_path.is_file():
            continue
        rel = str(file_path.relative_to(local_dir))
        if _should_exclude(rel):
            continue

        local_size = file_path.stat().st_size
        if existing.get(rel) == local_size:
            continue  # already up to date

        key = f"{prefix}/{rel}"
        _log.info("s3-sync up: %s → s3://%s/%s", rel, bucket, key)
        s3.upload_file(str(file_path), bucket, key)
        uploaded += 1

    return uploaded


def _download_sync(bucket: str, prefix: str, local_dir: Path) -> int:
    """Download new/changed S3 objects to local_dir. Returns number downloaded."""
    s3 = boto3.client("s3")

    downloaded = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(prefix) + 1:]
            if not rel:
                continue
            if _should_exclude(rel):
                continue

            local_file = local_dir / rel
            if local_file.exists() and local_file.stat().st_size == obj["Size"]:
                continue  # already up to date

            local_file.parent.mkdir(parents=True, exist_ok=True)
            _log.info("s3-sync down: s3://%s/%s → %s", bucket, key, rel)
            s3.download_file(bucket, key, str(local_file))
            downloaded += 1

    return downloaded


async def sync_to_s3(local_dir: Path, bucket: str, prefix: str) -> None:
    """Upload project files from local_dir to s3://bucket/prefix/. Non-blocking."""
    n = await asyncio.to_thread(_upload_sync, local_dir, bucket, prefix)
    _log.info("s3-sync up complete: %d file(s) → s3://%s/%s/", n, bucket, prefix)


async def sync_from_s3(bucket: str, prefix: str, local_dir: Path) -> None:
    """Download S3 objects under s3://bucket/prefix/ to local_dir. Non-blocking."""
    n = await asyncio.to_thread(_download_sync, bucket, prefix, local_dir)
    _log.info("s3-sync down complete: %d file(s) ← s3://%s/%s/", n, bucket, prefix)
