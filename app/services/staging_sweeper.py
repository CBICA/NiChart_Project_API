"""
Removal of staging areas that were never committed or discarded.

Every upload type stages its content under ``{project}/_upload/`` and expects a
follow-up call — a NIfTI commit, a DICOM convert, an intermediate-slot move — to
consume it, or an explicit DELETE to drop it. Nothing obliges a client to make
either call, so without a sweep the staged copies accumulate for the lifetime of
the deployment. For DICOM in particular that is the whole extracted series, which
is the largest thing an upload leaves behind.

``NICHART_STAGING_TTL_HOURS`` is the age at which a staging area becomes eligible
for removal; ``0`` disables sweeping entirely. The sweep runs once at startup and
then on a timer, from the application lifespan.

Age is read from each staging entry's own mtime. That tracks activity usefully
rather than just creation, because the operations a client performs on a staging
area — committing some of its files, sorting it into series subdirectories —
write to the staging directory itself and so bump its mtime. A staging area that
has sat untouched for the TTL is one nothing is doing anything with.
"""

import asyncio
import logging
import shutil
import time
from pathlib import Path

from app.services.path_security import PathEscapeError, assert_safe_path

_log = logging.getLogger(__name__)

# The staging roots under a project's ``_upload/``. Listed explicitly rather
# than discovered, so a future directory under ``_upload/`` is not swept by
# accident: anything whose lifetime is not "until the client commits it" must be
# added here deliberately.
_STAGING_ROOTS = (
    "nifti",        # NIfTI staging, consumed by the commit endpoint
    "dicoms",       # extracted DICOM staging, consumed by convert
    "intermediate",  # manual intermediate-slot staging (plus its *.zip)
    "_spool",       # per-request upload spool; normally removed by its own
                    # context manager, so an entry here outlived a crash
)

_SWEEP_INTERVAL_CEILING_SECONDS = 3600


def sweep_once(data_root: Path, ttl_hours: int) -> int:
    """Remove every staging entry older than *ttl_hours*. Returns the count.

    Blocking filesystem work — call it in a worker thread. Each removal target
    is contained against *data_root* before anything is unlinked, and only
    direct children of the known staging roots are considered, so a symlinked
    or otherwise unexpected entry cannot widen what gets deleted.

    Errors on individual entries are logged and skipped rather than raised: a
    sweep that cannot remove one project's staging area should still remove the
    rest.
    """
    if ttl_hours <= 0:
        return 0
    if not data_root.is_dir():
        return 0

    cutoff = time.time() - ttl_hours * 3600
    removed = 0

    for upload_dir in sorted(data_root.glob("*/*/_upload")):
        if not upload_dir.is_dir():
            continue
        for root_name in _STAGING_ROOTS:
            root = upload_dir / root_name
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                try:
                    assert_safe_path(data_root, entry)
                    if entry.is_symlink():
                        # Not a staging area this code created. Drop the link
                        # itself; never follow it.
                        entry.unlink()
                        removed += 1
                        continue
                    if entry.stat().st_mtime >= cutoff:
                        continue
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                except (PathEscapeError, OSError) as exc:
                    _log.warning("Could not sweep staging entry %s: %s", entry, exc)
                    continue
                removed += 1
                _log.info("Swept stale staging entry: %s", entry)

    return removed


async def sweep(data_root: Path, ttl_hours: int) -> int:
    """Run :func:`sweep_once` off the event loop."""
    return await asyncio.to_thread(sweep_once, data_root, ttl_hours)


async def run_periodically(data_root: Path, ttl_hours: int) -> None:
    """Sweep at startup, then once per interval until cancelled.

    The interval is an hour, or the TTL itself when that is shorter, so a
    deployment configured with a short TTL actually gets the behaviour it asked
    for instead of waiting an hour regardless.
    """
    interval = min(_SWEEP_INTERVAL_CEILING_SECONDS, max(60, ttl_hours * 3600))
    while True:
        try:
            removed = await sweep(data_root, ttl_hours)
            if removed:
                _log.info("Staging sweep removed %d stale entr%s.",
                          removed, "y" if removed == 1 else "ies")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failing sweep must not take the task down; the next tick retries.
            _log.warning("Staging sweep failed: %s", exc)
        await asyncio.sleep(interval)
