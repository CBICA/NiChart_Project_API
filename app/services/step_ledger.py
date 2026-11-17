"""
Step ledger: exactly how each of a project's current pipeline outputs was made.

``_provenance.json`` (one per output directory) says which pipeline step last
wrote a directory, but not what was run: the command, the mounts, the files
read. This ledger, ``{study_dir}/_working/step_ledger.json``, keeps one record
per executed step with everything needed to run it again by hand —

    run_id, pipeline_id, step_id, tool_id, image (+ digest when the backend
    reports one), backend, times, resolved params, the rendered command, each
    mount (project-relative host path, container path, mode, type), and the
    SHA-256 digests of what it read and wrote

— plus ``outputs``: project-relative output path → the record that last wrote
it. A step that later overwrites the same output replaces the mapping, so the
records reachable from ``outputs`` describe the project as it is now.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path

_log = logging.getLogger(__name__)

_LEDGER_FILE = "_working/step_ledger.json"

_lock = threading.Lock()


def execution_id(run_id: str, step_id: str) -> str:
    return f"{run_id}:{step_id}"


def load(study_dir: Path) -> dict:
    """``{"executions": {id: record}, "outputs": {rel_path: id}}`` (empty if none)."""
    try:
        data = json.loads((study_dir / _LEDGER_FILE).read_text())
        if isinstance(data, dict):
            data.setdefault("executions", {})
            data.setdefault("outputs", {})
            return data
    except (OSError, ValueError):
        pass
    return {"executions": {}, "outputs": {}}


def record_execution(study_dir: Path, record: dict) -> None:
    """Add a finished step's record and point its outputs at it. Never raises."""
    try:
        with _lock:
            data = load(study_dir)
            exec_id = record["execution_id"]
            data["executions"][exec_id] = record
            for rel in (record.get("outputs") or {}).values():
                data["outputs"][rel] = exec_id
            # Drop records no output points at any more: they describe files
            # that have since been overwritten.
            live = set(data["outputs"].values())
            data["executions"] = {k: v for k, v in data["executions"].items() if k in live}
            path = study_dir / _LEDGER_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(json.dumps(data, indent=1, default=str))
            tmp.replace(path)
    except Exception:
        _log.exception("step ledger: could not record %s in %s", record.get("execution_id"), study_dir)
