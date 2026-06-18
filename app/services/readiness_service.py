"""
Readiness service — checks whether a project has the data required by a pipeline.

Pipeline YAML ``requires`` items are either plain strings (e.g. ``needs_T1``,
``needs_FLAIR``) or single-key dicts (e.g. ``{csv_has_columns: [MRID, Age]}``).
This service parses both forms against the actual project directory.
"""

import csv
from pathlib import Path

from app.models.readiness import (
    ColumnCheck,
    CsvRequirement,
    ImagingRequirement,
    ReadinessReport,
)

# Map lowercase requires token → modality directory name
_MODALITY_TOKEN: dict[str, str] = {
    "needs_t1": "t1",
    "needs_t1w": "t1",
    "needs_fl": "fl",
    "needs_flair": "fl",
    "needs_t2": "t2",
    "needs_t1ce": "t1ce",
    "needs_adc": "adc",
}

_PARTICIPANTS_CSV = Path("participants") / "participants.csv"


def _count_subjects_in_dir(project_path: Path, modality_dir: str) -> int:
    d = project_path / modality_dir
    if not d.is_dir():
        return 0
    return sum(
        1 for f in d.iterdir()
        if f.is_file() and (f.name.endswith(".nii.gz") or f.suffix == ".nii")
    )


def _read_csv_coverage(project_path: Path) -> tuple[int, dict[str, list[str]]]:
    """Return ``(total_subjects, {column_name: [mrids_with_empty_value]})``.

    Only non-MRID columns are included in the second dict. The MRID column
    is matched case-insensitively. Returns ``(0, {})`` when the CSV is absent
    or has no MRID column.
    """
    csv_path = project_path / _PARTICIPANTS_CSV
    if not csv_path.exists():
        return 0, {}
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            return 0, {}
        col_map = {k.lower(): k for k in fieldnames}
        mrid_col = col_map.get("mrid")
        if not mrid_col:
            return 0, {}
        data_cols = [c for c in fieldnames if c != mrid_col]
        missing: dict[str, list[str]] = {c: [] for c in data_cols}
        total = 0
        for row in reader:
            mrid = (row.get(mrid_col) or "").strip()
            if not mrid:
                continue
            total += 1
            for col in data_cols:
                if not (row.get(col) or "").strip():
                    missing[col].append(mrid)
    return total, missing


def check_readiness(
    project_path: Path,
    pipeline_id: str,
    raw_requires: list,
) -> ReadinessReport:
    """Evaluate every requirement in ``raw_requires`` against the project directory."""
    imaging_checks: list[ImagingRequirement] = []
    csv_columns_required: list[str] = []

    for item in raw_requires:
        if isinstance(item, str):
            key = item.lower()
            mod_dir = _MODALITY_TOKEN.get(key)
            if mod_dir is not None:
                count = _count_subjects_in_dir(project_path, mod_dir)
                imaging_checks.append(ImagingRequirement(
                    modality=mod_dir,
                    subject_count=count,
                    satisfied=count > 0,
                ))
        elif isinstance(item, dict):
            cols = item.get("csv_has_columns") or []
            if isinstance(cols, list):
                csv_columns_required.extend(str(c) for c in cols)

    csv_req: CsvRequirement | None = None
    if csv_columns_required:
        total, col_missing = _read_csv_coverage(project_path)
        col_map = {k.lower(): k for k in col_missing}
        column_checks: list[ColumnCheck] = []
        csv_satisfied = True
        for req_col in csv_columns_required:
            actual = col_map.get(req_col.lower())
            if actual is None:
                column_checks.append(ColumnCheck(column=req_col, present=False, subjects_missing=[]))
                csv_satisfied = False
            else:
                missing_mrids = col_missing[actual]
                column_checks.append(ColumnCheck(
                    column=req_col,
                    present=True,
                    subjects_missing=missing_mrids,
                ))
                if missing_mrids:
                    csv_satisfied = False
        csv_req = CsvRequirement(
            required_columns=column_checks,
            total_subjects=total,
            satisfied=csv_satisfied,
        )

    all_ok = all(c.satisfied for c in imaging_checks)
    if csv_req is not None:
        all_ok = all_ok and csv_req.satisfied

    return ReadinessReport(
        pipeline_id=pipeline_id,
        satisfied=all_ok,
        imaging=imaging_checks,
        csv=csv_req,
    )
