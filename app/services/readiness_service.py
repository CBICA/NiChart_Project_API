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
    CompleteSetsRequirement,
    CsvRequirement,
    IdatRequirement,
    ImagingRequirement,
    ReadinessReport,
    SubjectCountRequirement,
)
from app import modalities
from app.services.file_service import collect_project_mrids

_PARTICIPANTS_CSV = Path("participants") / "participants.csv"


def _mrids_in_dir(project_path: Path, modality_dir: str) -> list[str]:
    """Return sorted list of MRID stems from NIfTI files in a modality directory."""
    d = project_path / modality_dir
    if not d.is_dir():
        return []
    mrids = []
    for f in d.iterdir():
        if not f.is_file():
            continue
        if f.name.endswith(".nii.gz"):
            mrids.append(f.name[:-7])
        elif f.suffix == ".nii":
            mrids.append(f.name[:-4])
    return sorted(mrids)


def _check_complete_sets(
    modality_mrids: dict[str, list[str]],
) -> CompleteSetsRequirement:
    """Given {modality: [mrids]}, compute which subjects are complete across all modalities."""
    required_modalities = sorted(modality_mrids)
    sets = [set(v) for v in modality_mrids.values()]
    complete = sorted(set.intersection(*sets)) if sets else []
    all_mrids = sorted(set.union(*sets)) if sets else []
    incomplete: dict[str, list[str]] = {}
    complete_set = set(complete)
    for mrid in all_mrids:
        if mrid in complete_set:
            continue
        missing_mods = [mod for mod, mrids in modality_mrids.items() if mrid not in mrids]
        incomplete[mrid] = sorted(missing_mods)
    return CompleteSetsRequirement(
        required_modalities=required_modalities,
        complete_mrids=complete,
        incomplete_mrids=incomplete,
        complete_count=len(complete),
        satisfied=len(complete) > 0,
    )


def _check_idat(project_path: Path) -> IdatRequirement:
    """Scan idat/ for paired {MRID}_Red.idat + {MRID}_Grn.idat files."""
    idat_dir = project_path / "idat"
    red_mrids: set[str] = set()
    grn_mrids: set[str] = set()
    if idat_dir.is_dir():
        for f in idat_dir.iterdir():
            if not f.is_file():
                continue
            name = f.name
            if name.endswith("_Red.idat"):
                red_mrids.add(name[:-9])
            elif name.endswith("_Grn.idat"):
                grn_mrids.add(name[:-9])
    complete = sorted(red_mrids & grn_mrids)
    missing_red = sorted(grn_mrids - red_mrids)
    missing_grn = sorted(red_mrids - grn_mrids)
    return IdatRequirement(
        complete_mrids=complete,
        missing_red=missing_red,
        missing_grn=missing_grn,
        complete_count=len(complete),
        satisfied=len(complete) > 0,
    )


def _read_csv_coverage(project_path: Path) -> tuple[int, dict[str, list[str]], set[str]]:
    """Return ``(total_subjects, {column_name: [mrids_with_empty_value]}, all_lowercase_columns)``.

    The third element is the set of all column names (lowercase) present in the CSV,
    including MRID. The second dict covers only non-MRID columns.
    Returns ``(0, {}, set())`` when the CSV is absent or has no MRID column.
    """
    csv_path = project_path / _PARTICIPANTS_CSV
    if not csv_path.exists():
        return 0, {}, set()
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            return 0, {}, set()
        all_cols_lower = {k.lower() for k in fieldnames}
        col_map = {k.lower(): k for k in fieldnames}
        mrid_col = col_map.get("mrid")
        if not mrid_col:
            return 0, {}, all_cols_lower
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
    return total, missing, all_cols_lower


def _validate_cell(value: str, schema: dict) -> bool:
    """Return True if value satisfies the column schema."""
    col_type = schema.get("type", "string")
    if col_type == "string":
        return True
    if col_type in ("int", "float"):
        try:
            num = float(value)
            if col_type == "int" and num != int(num):
                return False
            lo = schema.get("min")
            hi = schema.get("max")
            if lo is not None and num < lo:
                return False
            if hi is not None and num > hi:
                return False
        except (ValueError, TypeError):
            return False
    elif col_type == "categorical":
        allowed = [str(v) for v in (schema.get("values") or [])]
        if allowed and value not in allowed:
            return False
    return True


def _check_csv_values(
    project_path: Path,
    col_schemas: dict[str, dict],
) -> dict[str, list[str]]:
    """Return {column_name: [mrids_with_invalid_values]} for columns with non-string schemas."""
    schemas_to_check = {
        name: s for name, s in col_schemas.items()
        if s.get("type", "string") != "string" and name.lower() != "mrid"
    }
    if not schemas_to_check:
        return {}
    csv_path = project_path / _PARTICIPANTS_CSV
    if not csv_path.exists():
        return {}
    col_invalid: dict[str, list[str]] = {}
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            return {}
        col_map = {k.lower(): k for k in fieldnames}
        mrid_col = col_map.get("mrid")
        if not mrid_col:
            return {}
        for row in reader:
            mrid = (row.get(mrid_col) or "").strip()
            if not mrid:
                continue
            for col_name, schema in schemas_to_check.items():
                actual_col = col_map.get(col_name.lower())
                if not actual_col:
                    continue  # absent columns flagged separately
                value = (row.get(actual_col) or "").strip()
                if not value:
                    continue  # empty values flagged separately
                if not _validate_cell(value, schema):
                    col_invalid.setdefault(col_name, []).append(mrid)
    return col_invalid


def check_readiness(
    project_path: Path,
    pipeline_id: str,
    raw_requires: list,
) -> ReadinessReport:
    """Evaluate every requirement in ``raw_requires`` against the project directory."""
    imaging_checks: list[ImagingRequirement] = []
    needs_idat = False
    csv_columns_required: list[str] = []
    col_schemas: dict[str, dict] = {}
    min_subjects_spec: dict | None = None

    for item in raw_requires:
        if isinstance(item, str):
            key = item.lower()
            mod_dir = modalities.modality_for_requires(key)
            if mod_dir is not None:
                mrids = _mrids_in_dir(project_path, mod_dir)
                imaging_checks.append(ImagingRequirement(
                    modality=mod_dir,
                    subject_count=len(mrids),
                    mrids=mrids,
                    satisfied=len(mrids) > 0,
                ))
            elif key == "needs_idat":
                needs_idat = True
        elif isinstance(item, dict):
            cols = item.get("csv_has_columns") or []
            if isinstance(cols, list):
                for c in cols:
                    if isinstance(c, str):
                        csv_columns_required.append(c)
                    elif isinstance(c, dict) and c.get("name"):
                        name = str(c["name"])
                        csv_columns_required.append(name)
                        col_schemas[name] = c
            if "min_subjects" in item:
                min_subjects_spec = item["min_subjects"]

    # Cross-modality completeness check (only meaningful when ≥2 modalities required)
    complete_sets_check: CompleteSetsRequirement | None = None
    if len(imaging_checks) >= 2:
        modality_mrids = {c.modality: c.mrids for c in imaging_checks}
        complete_sets_check = _check_complete_sets(modality_mrids)

    idat_check: IdatRequirement | None = None
    if needs_idat:
        idat_check = _check_idat(project_path)

    csv_req: CsvRequirement | None = None
    if csv_columns_required:
        total, col_missing, all_cols_lower = _read_csv_coverage(project_path)
        col_invalid = _check_csv_values(project_path, col_schemas)
        col_map = {k.lower(): k for k in col_missing}
        column_checks: list[ColumnCheck] = []
        csv_satisfied = True
        for req_col in csv_columns_required:
            req_lower = req_col.lower()
            if req_lower not in all_cols_lower:
                column_checks.append(ColumnCheck(column=req_col, present=False, subjects_missing=[]))
                csv_satisfied = False
            elif req_lower == "mrid":
                column_checks.append(ColumnCheck(column=req_col, present=True, subjects_missing=[]))
            else:
                actual = col_map.get(req_lower)
                missing_mrids = col_missing.get(actual, []) if actual else []
                invalid_mrids = col_invalid.get(req_col, [])
                column_checks.append(ColumnCheck(
                    column=req_col,
                    present=True,
                    subjects_missing=missing_mrids,
                    subjects_invalid=invalid_mrids,
                ))
                if missing_mrids or invalid_mrids:
                    csv_satisfied = False
        csv_req = CsvRequirement(
            required_columns=column_checks,
            total_subjects=total,
            satisfied=csv_satisfied,
        )

    subject_count_check: SubjectCountRequirement | None = None
    if min_subjects_spec is not None:
        required = int(min_subjects_spec.get("required", 1))
        recommended = int(min_subjects_spec.get("recommended", required))
        actual = len(collect_project_mrids(project_path))
        subject_count_check = SubjectCountRequirement(
            actual=actual,
            required=required,
            recommended=recommended,
            satisfied=actual >= required,
            recommended_met=actual >= recommended,
        )

    all_ok = all(c.satisfied for c in imaging_checks)
    if complete_sets_check is not None:
        all_ok = all_ok and complete_sets_check.satisfied
    if idat_check is not None:
        all_ok = all_ok and idat_check.satisfied
    if csv_req is not None:
        all_ok = all_ok and csv_req.satisfied
    if subject_count_check is not None:
        all_ok = all_ok and subject_count_check.satisfied

    return ReadinessReport(
        pipeline_id=pipeline_id,
        satisfied=all_ok,
        imaging=imaging_checks,
        complete_sets=complete_sets_check,
        idat=idat_check,
        csv=csv_req,
        subject_count=subject_count_check,
    )
