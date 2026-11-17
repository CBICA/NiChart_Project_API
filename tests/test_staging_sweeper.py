"""
Tests for the staging TTL sweep (``app.services.staging_sweeper``).

``NICHART_STAGING_TTL_HOURS`` was configured and documented long before anything
read it, so these cover both that stale staging areas are now actually removed
and that the sweep cannot reach anything else.
"""

import asyncio
import os
import time
from pathlib import Path

import pytest

from app.services import staging_sweeper

_TTL_HOURS = 24


def _aged(path: Path, hours: float) -> Path:
    """Backdate *path*'s mtime by *hours*."""
    when = time.time() - hours * 3600
    os.utime(path, (when, when))
    return path


def _project(data_root: Path, user="LOCAL_USER", project="p1") -> Path:
    p = data_root / user / project
    p.mkdir(parents=True, exist_ok=True)
    return p


def _staging(project: Path, root: str, name: str, *, hours: float) -> Path:
    d = project / "_upload" / root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "staged.nii.gz").write_bytes(b"x")
    return _aged(d, hours)


# ── What gets swept ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("root", ["nifti", "dicoms", "intermediate", "_spool"])
def test_stale_staging_is_removed_from_every_root(tmp_path, root):
    project = _project(tmp_path)
    stale = _staging(project, root, "abc123", hours=_TTL_HOURS + 1)

    assert staging_sweeper.sweep_once(tmp_path, _TTL_HOURS) == 1
    assert not stale.exists()


def test_fresh_staging_is_left_alone(tmp_path):
    project = _project(tmp_path)
    fresh = _staging(project, "dicoms", "fresh", hours=1)

    assert staging_sweeper.sweep_once(tmp_path, _TTL_HOURS) == 0
    assert (fresh / "staged.nii.gz").exists()


def test_sweep_spans_every_user_and_project(tmp_path):
    a = _staging(_project(tmp_path, "user-a", "p1"), "nifti", "s1", hours=_TTL_HOURS + 1)
    b = _staging(_project(tmp_path, "user-b", "p2"), "dicoms", "s2", hours=_TTL_HOURS + 1)
    fresh = _staging(_project(tmp_path, "user-b", "p3"), "nifti", "s3", hours=0)

    assert staging_sweeper.sweep_once(tmp_path, _TTL_HOURS) == 2
    assert not a.exists() and not b.exists()
    assert fresh.exists()


def test_leftover_intermediate_archive_is_swept(tmp_path):
    """The intermediate path writes ``{staging}.zip`` beside its staging dir."""
    project = _project(tmp_path)
    root = project / "_upload" / "intermediate"
    root.mkdir(parents=True)
    leftover = root / "abc123.zip"
    leftover.write_bytes(b"PK")
    _aged(leftover, _TTL_HOURS + 1)

    assert staging_sweeper.sweep_once(tmp_path, _TTL_HOURS) == 1
    assert not leftover.exists()


# ── What the sweep must never touch ───────────────────────────────────────────

def test_project_data_is_never_swept(tmp_path):
    """Only ``_upload`` staging roots are in scope — not committed images."""
    project = _project(tmp_path)
    committed = project / "t1" / "sub001.nii.gz"
    committed.parent.mkdir(parents=True)
    committed.write_bytes(b"image")
    _aged(committed, _TTL_HOURS * 10)
    _aged(committed.parent, _TTL_HOURS * 10)

    working = project / "_working" / "metadata.json"
    working.parent.mkdir(parents=True)
    working.write_text("{}")
    _aged(working, _TTL_HOURS * 10)

    assert staging_sweeper.sweep_once(tmp_path, _TTL_HOURS) == 0
    assert committed.exists()
    assert working.exists()


def test_unknown_upload_subdirectory_is_not_swept(tmp_path):
    """A directory added under ``_upload/`` later must be opted in explicitly."""
    project = _project(tmp_path)
    other = project / "_upload" / "something_new" / "keep"
    other.mkdir(parents=True)
    _aged(other, _TTL_HOURS * 10)
    _aged(other.parent, _TTL_HOURS * 10)

    assert staging_sweeper.sweep_once(tmp_path, _TTL_HOURS) == 0
    assert other.exists()


def test_symlinked_staging_entry_is_unlinked_not_followed(tmp_path):
    """A link where a staging area should be loses the link, not the target."""
    project = _project(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    (target / "precious.txt").write_text("keep")

    root = project / "_upload" / "nifti"
    root.mkdir(parents=True)
    link = root / "sneaky"
    link.symlink_to(target)

    staging_sweeper.sweep_once(tmp_path, _TTL_HOURS)
    assert not link.exists()
    assert (target / "precious.txt").read_text() == "keep"


# ── Configuration ─────────────────────────────────────────────────────────────

def test_ttl_of_zero_disables_the_sweep(tmp_path):
    project = _project(tmp_path)
    stale = _staging(project, "nifti", "s1", hours=_TTL_HOURS * 100)

    assert staging_sweeper.sweep_once(tmp_path, 0) == 0
    assert stale.exists()


def test_missing_data_root_is_not_an_error(tmp_path):
    assert staging_sweeper.sweep_once(tmp_path / "nope", _TTL_HOURS) == 0


def test_sweep_runs_off_the_event_loop(tmp_path):
    project = _project(tmp_path)
    stale = _staging(project, "dicoms", "s1", hours=_TTL_HOURS + 1)

    assert asyncio.run(staging_sweeper.sweep(tmp_path, _TTL_HOURS)) == 1
    assert not stale.exists()
