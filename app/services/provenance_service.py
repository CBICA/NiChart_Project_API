"""
Provenance verification service.

Scans a project directory for _provenance.json files written by the job service
after each successful pipeline step, then checks whether any recorded input paths
have been modified since the step finished.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from app.models.provenance import (
    ProvenanceEntry,
    ProvenanceInputCheck,
    ProvenanceReport,
)

_PROVENANCE_FILENAME = "_provenance.json"
_EXCLUDE_DIRS = {"_upload", "_working"}


def _parse_ts(iso: str) -> float:
    """Parse an ISO timestamp string to a UTC Unix timestamp."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _check_input(label: str, path_str: str, ref_ts: float) -> ProvenanceInputCheck:
    """Check one input path against the step's finish timestamp."""
    p = Path(path_str)

    if not p.exists():
        return ProvenanceInputCheck(label=label, path=path_str, status="missing")

    modified_count = 0
    if p.is_file():
        if p.stat().st_mtime > ref_ts:
            modified_count = 1
    elif p.is_dir():
        for f in p.rglob("*"):
            if f.is_file() and f.name != _PROVENANCE_FILENAME and f.stat().st_mtime > ref_ts:
                modified_count += 1

    status = "modified" if modified_count > 0 else "clean"
    return ProvenanceInputCheck(
        label=label,
        path=path_str,
        status=status,
        modified_count=modified_count,
    )


def _entry_from_file(project_path: Path, prov_file: Path) -> ProvenanceEntry:
    """Parse one _provenance.json and check all its input paths."""
    output_dir = str(prov_file.parent.relative_to(project_path))

    try:
        data = json.loads(prov_file.read_text())
    except Exception as exc:
        return ProvenanceEntry(
            output_dir=output_dir,
            pipeline_id="",
            step_id="",
            container_image="",
            generated_at="",
            inputs=[],
            overall="unreadable",
            error=str(exc),
        )

    generated_at: str = data.get("generated_at", "")
    try:
        ref_ts = _parse_ts(generated_at)
    except (ValueError, TypeError):
        return ProvenanceEntry(
            output_dir=output_dir,
            pipeline_id=data.get("pipeline_id", ""),
            step_id=data.get("step_id", ""),
            container_image=data.get("container_image", ""),
            generated_at=generated_at,
            inputs=[],
            overall="unreadable",
            error=f"Cannot parse generated_at: {generated_at!r}",
        )

    input_paths: dict = data.get("input_paths") or {}
    input_checks = [
        _check_input(label, path_str, ref_ts)
        for label, path_str in input_paths.items()
    ]

    if any(c.status == "missing" for c in input_checks):
        overall = "missing_inputs"
    elif any(c.status == "modified" for c in input_checks):
        overall = "dirty"
    else:
        overall = "clean"

    return ProvenanceEntry(
        output_dir=output_dir,
        pipeline_id=data.get("pipeline_id", ""),
        step_id=data.get("step_id", ""),
        container_image=data.get("container_image", ""),
        generated_at=generated_at,
        execution_mode=data.get("execution_mode", ""),
        user_id=data.get("user_id", ""),
        backend=data.get("backend", ""),
        inputs=input_checks,
        overall=overall,
    )


def verify_provenance(project_path: Path, project_id: str) -> ProvenanceReport:
    """Scan all _provenance.json files in a project and check input staleness."""
    entries: list[ProvenanceEntry] = []

    for prov_file in sorted(project_path.rglob(_PROVENANCE_FILENAME)):
        # Skip staging and working internals
        parts = set(prov_file.relative_to(project_path).parts)
        if parts & _EXCLUDE_DIRS:
            continue
        entries.append(_entry_from_file(project_path, prov_file))

    if not entries:
        summary = "no_provenance"
    elif all(e.overall == "clean" for e in entries):
        summary = "all_clean"
    else:
        summary = "some_dirty"

    return ProvenanceReport(project_id=project_id, entries=entries, summary=summary)
