"""
Ingestion log: where each of a project's raw input files came from, and when.

Pipeline outputs carry their own provenance (``_provenance.json`` and the step
ledger). Raw inputs — images, participants.csv, IDAT files — have nothing like
it: they are written by several upload paths and edited in place. This log,
``{study_dir}/_working/ingest_log.json``, records every change to them as an
event (``nifti_upload``, ``participants_edit``, ...) listing the files added,
changed or removed, each with its SHA-256.

Usage, around anything that writes input data::

    with ingest_log.tracked(project_path, "nifti_upload", {"staging_id": ...}):
        ...write files...

``begin`` (entering) first absorbs any change made outside a tracked operation
into an ``untracked_change`` event, so it is never attributed to the upload
that follows. The first time a project is seen, its existing inputs are
recorded as a ``baseline`` event instead: they predate this log, and their
times are only the files' modification times.

Logging must never break an upload: every public function swallows and logs
its own errors.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.modalities import MODALITIES
from app.services.file_hashes import HashCache, list_files

_log = logging.getLogger(__name__)

_LOG_FILE = "_working/ingest_log.json"

# Project-relative roots that hold raw input data (as opposed to pipeline outputs).
INPUT_ROOTS: tuple[str, ...] = tuple(m.dir_name for m in MODALITIES) + ("participants", "idat")

INPUT_ROOT_LABELS: dict[str, str] = {
    **{m.dir_name: f"{m.label} images" for m in MODALITIES},
    "participants": "Participant information",
    "idat": "IDAT (methylation array) files",
}

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(study_dir: Path) -> dict | None:
    try:
        data = json.loads((study_dir / _LOG_FILE).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _save(study_dir: Path, data: dict) -> None:
    path = study_dir / _LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def load(study_dir: Path) -> dict:
    """The log as stored: ``{"files": {rel: {...}}, "events": [...]}`` (empty if none)."""
    return _load(study_dir) or {"files": {}, "events": []}


def _scan(study_dir: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for root in INPUT_ROOTS:
        for f in list_files(study_dir, root):
            files[f.relative_to(study_dir).as_posix()] = f
    return files


def _diff(study_dir: Path, data: dict, hashes: HashCache) -> tuple[dict, list, list, list]:
    """Current state of the input files vs the log: (current, added, changed, removed)."""
    known: dict = data.get("files", {})
    current: dict[str, dict] = {}
    added, changed = [], []
    for rel, f in _scan(study_dir).items():
        try:
            fh = hashes.hash(f)
            mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            continue
        current[rel] = {"size": fh.size, "sha256": fh.sha256, "mtime": mtime}
        if rel not in known:
            added.append(rel)
        elif known[rel].get("sha256") != fh.sha256:
            changed.append(rel)
    removed = [rel for rel in known if rel not in current]
    return current, added, changed, removed


def _apply(data: dict, current: dict, kind: str, detail: dict | None,
           added: list, changed: list, removed: list) -> dict | None:
    if not (added or changed or removed):
        return None
    event = {
        "event_id": uuid.uuid4().hex[:12],
        "kind": kind,
        "at": _now(),
        "detail": detail or {},
        "added": sorted(added),
        "changed": sorted(changed),
        "removed": sorted(removed),
    }
    files = data.setdefault("files", {})
    for rel in added + changed:
        files[rel] = {**current[rel], "event_id": event["event_id"], "recorded_at": event["at"]}
    for rel in removed:
        files.pop(rel, None)
    data.setdefault("events", []).append(event)
    return event


def begin(study_dir: Path) -> None:
    """Absorb changes made outside a tracked operation (or seed the baseline)."""
    try:
        with _lock:
            hashes = HashCache(study_dir)
            existing = _load(study_dir)
            data = existing or {"files": {}, "events": []}
            current, added, changed, removed = _diff(study_dir, data, hashes)
            kind = "baseline" if existing is None else "untracked_change"
            event = _apply(data, current, kind, None, added, changed, removed)
            if event is not None or existing is None:
                _save(study_dir, data)
            hashes.save()
    except Exception:
        _log.exception("ingest log: begin failed for %s", study_dir)


def commit(study_dir: Path, kind: str, detail: dict | None = None) -> dict | None:
    """Record the input changes since ``begin`` as one *kind* event; returns it (or None)."""
    try:
        with _lock:
            hashes = HashCache(study_dir)
            data = load(study_dir)
            current, added, changed, removed = _diff(study_dir, data, hashes)
            event = _apply(data, current, kind, detail, added, changed, removed)
            if event is not None:
                _save(study_dir, data)
            hashes.save()
            return event
    except Exception:
        _log.exception("ingest log: commit (%s) failed for %s", kind, study_dir)
        return None


@contextmanager
def tracked(study_dir: Path, kind: str, detail: dict | None = None) -> Iterator[None]:
    """Record whatever the block changes in the project's inputs as one event.

    Recorded even when the block raises: files it wrote before failing are on
    disk and must be accounted for.
    """
    begin(study_dir)
    try:
        yield
    finally:
        commit(study_dir, kind, detail)
