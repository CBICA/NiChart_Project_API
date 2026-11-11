"""
Tests for readiness evaluation, focused on ``min_subjects.constraints`` —
per-cohort minimum counts such as "at least 30 subjects with DX == CN".

check_readiness() is a pure function over a project directory, so these build a
project on disk rather than going through the HTTP layer.
"""

import csv

from app.services import readiness_service

CN_CONTROLS = [{"label": "Cognitively normal controls", "where": [{"column": "DX", "equals": "CN"}]}]


def _write_imaging(project, mrids):
    d = project / "t1"
    d.mkdir(parents=True, exist_ok=True)
    for mrid in mrids:
        (d / f"{mrid}.nii.gz").write_bytes(b"x")


def _write_participants(project, rows, columns=("MRID", "DX", "Age", "Sex")):
    d = project / "participants"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "participants.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _requires(required=30, recommended=30, constraints=None):
    spec = {"required": required, "recommended": recommended}
    if constraints is not None:
        spec["constraints"] = constraints
    return ["needs_T1", {"min_subjects": spec}]


def _cohort(project, n_cn, n_other, dx_value="CN", extra=None):
    """Build a project with n_cn CN subjects and n_other non-CN subjects."""
    rows = []
    for i in range(n_cn):
        rows.append({"MRID": f"cn{i:03d}", "DX": dx_value, "Age": "70", "Sex": "M", **(extra or {})})
    for i in range(n_other):
        rows.append({"MRID": f"ad{i:03d}", "DX": "AD", "Age": "72", "Sex": "F", **(extra or {})})
    _write_imaging(project, [r["MRID"] for r in rows])
    _write_participants(project, rows)
    return rows


def _check(project, requires):
    return readiness_service.check_readiness(project, "run_dlmuse_harmonized", requires)


# ── The headline case ─────────────────────────────────────────────────────────

def test_constraint_satisfied_when_enough_subjects_match(tmp_path):
    _cohort(tmp_path, n_cn=30, n_other=10)

    report = _check(tmp_path, _requires(constraints=CN_CONTROLS))
    sc = report.subject_count

    assert sc.actual == 40            # total, unconstrained
    constraint = sc.constraints[0]
    assert constraint.label == "Cognitively normal controls"
    assert constraint.actual == 30
    assert constraint.satisfied and constraint.recommended_met
    assert constraint.unevaluable_reason is None
    assert len(constraint.matching_mrids) == 30
    assert all(m.startswith("cn") for m in constraint.matching_mrids)
    assert sc.satisfied and report.satisfied


def test_constraint_fails_even_when_total_is_plentiful(tmp_path):
    """200 subjects but only 10 controls must not read as ready."""
    _cohort(tmp_path, n_cn=10, n_other=190)

    sc = _check(tmp_path, _requires(constraints=CN_CONTROLS)).subject_count

    assert sc.actual == 200
    assert sc.constraints[0].actual == 10
    assert not sc.constraints[0].satisfied
    # The parent flag folds in constraints so a client can gate on it alone.
    assert not sc.satisfied
    assert not sc.recommended_met


# ── Independent parent / child thresholds ─────────────────────────────────────

def test_total_and_cohort_thresholds_are_independent(tmp_path):
    """Expresses '100 total, at least 30 CN'."""
    requires = _requires(
        required=100, recommended=100,
        constraints=[{"required": 30, "where": [{"column": "DX", "equals": "CN"}]}],
    )

    # Cohort met, total short.
    _cohort(tmp_path, n_cn=30, n_other=20)
    sc = _check(tmp_path, requires).subject_count
    assert sc.actual == 50
    assert sc.constraints[0].satisfied      # 30 >= 30
    assert not sc.satisfied                 # 50 < 100


def test_both_thresholds_met(tmp_path):
    requires = _requires(
        required=100, recommended=100,
        constraints=[{"required": 30, "where": [{"column": "DX", "equals": "CN"}]}],
    )
    _cohort(tmp_path, n_cn=30, n_other=70)

    sc = _check(tmp_path, requires).subject_count

    assert sc.actual == 100
    assert sc.constraints[0].actual == 30
    assert sc.satisfied and sc.recommended_met


def test_constraint_thresholds_default_sensibly(tmp_path):
    _cohort(tmp_path, n_cn=5, n_other=5)

    # No required/recommended on the constraint → inherits the parent's 30/30.
    sc = _check(tmp_path, _requires(required=30, recommended=30, constraints=CN_CONTROLS)).subject_count
    assert (sc.constraints[0].required, sc.constraints[0].recommended) == (30, 30)

    # required given, recommended omitted → recommended tracks its own required.
    sc = _check(tmp_path, _requires(
        required=10, recommended=99,
        constraints=[{"required": 4, "where": [{"column": "DX", "equals": "CN"}]}],
    )).subject_count
    assert (sc.constraints[0].required, sc.constraints[0].recommended) == (4, 4)


# ── Matching semantics ────────────────────────────────────────────────────────

def test_string_matching_is_case_and_whitespace_insensitive(tmp_path):
    rows = [
        {"MRID": "a", "DX": "CN", "Age": "70", "Sex": "M"},
        {"MRID": "b", "DX": "cn", "Age": "70", "Sex": "M"},
        {"MRID": "c", "DX": " Cn ", "Age": "70", "Sex": "M"},
        {"MRID": "d", "DX": "AD", "Age": "70", "Sex": "M"},
    ]
    _write_imaging(tmp_path, [r["MRID"] for r in rows])
    _write_participants(tmp_path, rows)

    constraint = _check(tmp_path, _requires(
        required=1, constraints=[{"required": 3, "where": [{"column": "dx", "equals": "cn"}]}],
    )).subject_count.constraints[0]

    assert constraint.matching_mrids == ["a", "b", "c"]
    assert constraint.satisfied


def test_in_and_numeric_range_predicates(tmp_path):
    rows = [
        {"MRID": "a", "DX": "CN", "Age": "65", "Sex": "M"},
        {"MRID": "b", "DX": "MCI", "Age": "80", "Sex": "F"},
        {"MRID": "c", "DX": "CN", "Age": "40", "Sex": "M"},   # too young
        {"MRID": "d", "DX": "AD", "Age": "70", "Sex": "F"},   # excluded DX
    ]
    _write_imaging(tmp_path, [r["MRID"] for r in rows])
    _write_participants(tmp_path, rows)

    constraint = _check(tmp_path, _requires(
        required=1,
        constraints=[{
            "required": 2,
            "where": [
                {"column": "DX", "in": ["CN", "MCI"]},
                {"column": "Age", "min": 60, "max": 90},
            ],
        }],
    )).subject_count.constraints[0]

    assert constraint.matching_mrids == ["a", "b"]
    assert constraint.satisfied
    # The predicate is echoed back for display, with 'in' keyed as written.
    assert constraint.where[0].in_ == ["CN", "MCI"]
    assert constraint.where[1].min == 60


def test_empty_cells_and_rowless_subjects_do_not_count(tmp_path):
    rows = [
        {"MRID": "a", "DX": "CN", "Age": "70", "Sex": "M"},
        {"MRID": "b", "DX": "", "Age": "70", "Sex": "M"},      # blank DX
    ]
    # 'c' has imaging but no covariate row at all.
    _write_imaging(tmp_path, ["a", "b", "c"])
    _write_participants(tmp_path, rows)

    sc = _check(tmp_path, _requires(required=1, constraints=CN_CONTROLS)).subject_count

    assert sc.actual == 3                       # total counts imaging
    assert sc.constraints[0].matching_mrids == ["a"]


def test_csv_row_without_imaging_does_not_count(tmp_path):
    rows = [{"MRID": f"cn{i}", "DX": "CN", "Age": "70", "Sex": "M"} for i in range(5)]
    _write_imaging(tmp_path, ["cn0", "cn1"])    # only two actually have scans
    _write_participants(tmp_path, rows)

    constraint = _check(tmp_path, _requires(required=1, constraints=CN_CONTROLS)).subject_count.constraints[0]

    assert constraint.matching_mrids == ["cn0", "cn1"]
    assert constraint.actual == 2


# ── Unevaluable cohorts ───────────────────────────────────────────────────────

def test_missing_column_is_unevaluable_not_zero(tmp_path):
    """'No DX column' must be distinguishable from 'nobody is CN'."""
    rows = [{"MRID": "a", "Age": "70", "Sex": "M"}]
    _write_imaging(tmp_path, ["a"])
    _write_participants(tmp_path, rows, columns=("MRID", "Age", "Sex"))

    constraint = _check(tmp_path, _requires(required=1, constraints=CN_CONTROLS)).subject_count.constraints[0]

    assert constraint.actual == 0
    assert not constraint.satisfied
    assert constraint.unevaluable_reason is not None
    assert "DX" in constraint.unevaluable_reason


def test_missing_participants_csv_is_unevaluable(tmp_path):
    _write_imaging(tmp_path, ["a", "b"])

    constraint = _check(tmp_path, _requires(required=1, constraints=CN_CONTROLS)).subject_count.constraints[0]

    assert constraint.unevaluable_reason is not None
    assert not constraint.satisfied


def test_constraint_without_conditions_is_unevaluable(tmp_path):
    _cohort(tmp_path, n_cn=5, n_other=0)

    constraint = _check(tmp_path, _requires(
        required=1, constraints=[{"label": "broken", "where": []}],
    )).subject_count.constraints[0]

    assert constraint.unevaluable_reason is not None
    assert not constraint.satisfied


# ── Backwards compatibility ───────────────────────────────────────────────────

def test_min_subjects_without_constraints_is_unchanged(tmp_path):
    _cohort(tmp_path, n_cn=2, n_other=3)

    sc = _check(tmp_path, _requires(required=5, recommended=5)).subject_count

    assert sc.constraints == []
    assert sc.actual == 5
    assert sc.satisfied and sc.recommended_met


# ── Per-group minimums (``per``) ──────────────────────────────────────────────

CN_PER_BATCH = [{**CN_CONTROLS[0], "per": "Batch"}]


def _batched(project, batches):
    """batches: {batch: (n_cn, n_other)} → project with a Batch column."""
    rows = []
    for batch, (n_cn, n_other) in batches.items():
        for i in range(n_cn):
            rows.append({"MRID": f"{batch}_cn{i:03d}", "DX": "CN", "Batch": batch})
        for i in range(n_other):
            rows.append({"MRID": f"{batch}_ad{i:03d}", "DX": "AD", "Batch": batch})
    _write_imaging(project, [r["MRID"] for r in rows])
    _write_participants(project, rows, columns=("MRID", "DX", "Batch"))


def test_per_batch_fails_when_one_batch_is_short(tmp_path):
    """40 controls in total pass a project-wide minimum, but not 20 + 20 per batch."""
    _batched(tmp_path, {"A": (20, 5), "B": (20, 5)})
    c = _check(tmp_path, _requires(constraints=CN_PER_BATCH)).subject_count.constraints[0]

    assert c.per == "Batch"
    assert [(g.value, g.actual, g.satisfied) for g in c.groups] == [("A", 20, False), ("B", 20, False)]
    assert c.actual == 20               # smallest group
    assert not c.satisfied
    assert len(c.matching_mrids) == 40  # still every matching subject


def test_per_batch_satisfied_when_every_batch_has_enough(tmp_path):
    _batched(tmp_path, {"A": (30, 0), "B": (35, 10)})
    report = _check(tmp_path, _requires(constraints=CN_PER_BATCH))
    c = report.subject_count.constraints[0]

    assert c.actual == 30
    assert all(g.satisfied for g in c.groups)
    assert c.satisfied and report.subject_count.satisfied


def test_per_batch_includes_batches_without_matches(tmp_path):
    _batched(tmp_path, {"A": (30, 0), "B": (0, 12)})
    c = _check(tmp_path, _requires(constraints=CN_PER_BATCH)).subject_count.constraints[0]

    assert [(g.value, g.actual) for g in c.groups] == [("A", 30), ("B", 0)]
    assert c.actual == 0 and not c.satisfied


def test_per_column_missing_is_unevaluable(tmp_path):
    _cohort(tmp_path, n_cn=40, n_other=0)  # no Batch column
    c = _check(tmp_path, _requires(constraints=CN_PER_BATCH)).subject_count.constraints[0]

    assert c.unevaluable_reason and "'Batch'" in c.unevaluable_reason
    assert not c.satisfied
    assert c.groups == []


def test_without_per_groups_is_empty(tmp_path):
    _cohort(tmp_path, n_cn=30, n_other=0)
    c = _check(tmp_path, _requires(constraints=CN_CONTROLS)).subject_count.constraints[0]
    assert c.per is None and c.groups == []


# ── participants.csv measured against the subjects being processed ───────────
#
# The CSV check used to judge the file on its own rows, so a project whose CSV
# described a different cohort read as ready: every row it had was complete, and
# an imaging subject with no row at all was never counted as missing anything.

def _write_modality(project, modality, mrids):
    d = project / modality
    d.mkdir(parents=True, exist_ok=True)
    for mrid in mrids:
        (d / f"{mrid}.nii.gz").write_bytes(b"x")


def _csv_requires(*columns, needs=("needs_T1",)):
    return [*needs, {"csv_has_columns": list(columns)}]


def test_imaging_subjects_with_no_row_are_not_ready(tmp_path):
    """The reported case: two imaging subjects, 588 rows for a different cohort."""
    _write_imaging(tmp_path, ["subject001", "subject002"])
    _write_participants(
        tmp_path,
        [{"MRID": f"IXI{i:03d}-Guys-0828-T1", "DX": "CN", "Age": "70", "Sex": "M"}
         for i in range(588)],
    )

    report = _check(tmp_path, _csv_requires("MRID", "Age", "Sex"))

    assert report.csv.subjects_without_row == ["subject001", "subject002"]
    assert not report.csv.satisfied
    assert not report.satisfied
    # Every required column is missing for both, by the same cause.
    by_col = {c.column: c for c in report.csv.required_columns}
    assert by_col["Age"].present and by_col["Age"].subjects_missing == [
        "subject001", "subject002",
    ]
    # The file itself is still described as it was.
    assert report.csv.total_subjects == 588
    assert report.csv.rows_without_images == 588


def test_partially_covered_imaging_is_not_ready(tmp_path):
    _write_imaging(tmp_path, ["sub001", "sub002"])
    _write_participants(tmp_path, [{"MRID": "sub001", "DX": "CN", "Age": "70", "Sex": "M"}])

    report = _check(tmp_path, _csv_requires("MRID", "Age", "Sex"))

    assert report.csv.subjects_without_row == ["sub002"]
    assert not report.csv.satisfied
    assert not report.satisfied


def test_extra_rows_do_not_make_a_covered_project_unready(tmp_path):
    """A shared demographics sheet is normal: rows for subjects with no images
    are counted and reported, never held against the pipeline."""
    _write_imaging(tmp_path, ["sub001", "sub002"])
    rows = [
        {"MRID": "sub001", "DX": "CN", "Age": "70", "Sex": "M"},
        {"MRID": "sub002", "DX": "CN", "Age": "71", "Sex": "F"},
    ]
    rows += [{"MRID": f"other{i:03d}", "DX": "CN", "Age": "", "Sex": ""} for i in range(500)]
    _write_participants(tmp_path, rows)

    report = _check(tmp_path, _csv_requires("MRID", "Age", "Sex"))

    assert report.csv.satisfied
    assert report.csv.subjects_without_row == []
    assert report.csv.rows_without_images == 500
    assert report.csv.total_subjects == 502
    assert all(not c.subjects_missing for c in report.csv.required_columns)
    # The blanks in those rows are reported for the client to explain, not held against it.
    assert report.csv.evaluated_subjects == 2
    by_col = {c.column: c for c in report.csv.required_columns}
    assert by_col["Age"].missing_without_images == 500
    assert by_col["Sex"].missing_without_images == 500


def test_coverage_counts_are_over_the_processed_subjects(tmp_path):
    _write_imaging(tmp_path, ["sub001", "sub002", "sub003"])
    _write_participants(tmp_path, [
        {"MRID": "sub001", "DX": "CN", "Age": "70", "Sex": "M"},
        {"MRID": "sub002", "DX": "CN", "Age": "", "Sex": "F"},
        {"MRID": "sub003", "DX": "CN", "Age": "72", "Sex": ""},
        {"MRID": "noimg", "DX": "CN", "Age": "", "Sex": "M"},
    ])

    report = _check(tmp_path, _csv_requires("MRID", "Age", "Sex"))

    assert report.csv.evaluated_subjects == 3
    by_col = {c.column: c for c in report.csv.required_columns}
    assert by_col["Age"].subjects_missing == ["sub002"]
    assert by_col["Age"].missing_without_images == 1
    assert by_col["Sex"].subjects_missing == ["sub003"]
    assert by_col["Sex"].missing_without_images == 0


def test_only_complete_sets_are_judged_for_a_multi_modality_pipeline(tmp_path):
    """A subject with T1 but no FLAIR will not be processed, so its absent row
    is not a reason to block the run."""
    _write_modality(tmp_path, "t1", ["both01", "t1only"])
    _write_modality(tmp_path, "fl", ["both01"])
    _write_participants(tmp_path, [{"MRID": "both01", "DX": "CN", "Age": "70", "Sex": "M"}])

    report = _check(
        tmp_path, _csv_requires("MRID", "Age", "Sex", needs=("needs_T1", "needs_FLAIR")),
    )

    assert report.csv.subjects_without_row == []
    assert report.csv.satisfied
    assert report.complete_sets.complete_mrids == ["both01"]
    assert report.satisfied


def test_no_imaging_yet_does_not_add_a_second_failure(tmp_path):
    """The imaging check already fails; the CSV must not pile on a row-less
    verdict for subjects that do not exist."""
    _write_participants(tmp_path, [{"MRID": "sub001", "DX": "CN", "Age": "70", "Sex": "M"}])

    report = _check(tmp_path, _csv_requires("MRID", "Age", "Sex"))

    assert report.csv.subjects_without_row == []
    assert report.csv.satisfied
    assert not report.imaging[0].satisfied
    assert not report.satisfied


def test_a_pipeline_without_imaging_is_still_judged_on_its_own_rows(tmp_path):
    """With no imaging requirement there is no population to compare against, so
    a blank required value in the CSV itself must still fail."""
    _write_participants(tmp_path, [
        {"MRID": "sub001", "DX": "CN", "Age": "70", "Sex": "M"},
        {"MRID": "sub002", "DX": "CN", "Age": "", "Sex": "F"},
    ])

    report = readiness_service.check_readiness(
        tmp_path, "csv_only", [{"csv_has_columns": ["MRID", "Age"]}]
    )

    assert report.csv.subjects_without_row == []
    assert report.csv.rows_without_images == 0
    assert [c.subjects_missing for c in report.csv.required_columns if c.column == "Age"] == [
        ["sub002"]
    ]
    assert not report.csv.satisfied


def test_an_absent_csv_reports_no_rowless_subjects(tmp_path):
    """The remedy is to upload a file, not to correct MRIDs, so the row-less list
    stays empty and the missing MRID column carries the failure."""
    _write_imaging(tmp_path, ["sub001"])

    report = _check(tmp_path, _csv_requires("MRID", "Age"))

    assert report.csv.subjects_without_row == []
    assert not report.csv.satisfied
    assert {c.column: c.present for c in report.csv.required_columns} == {
        "MRID": False, "Age": False,
    }


def test_invalid_values_are_restricted_to_processed_subjects(tmp_path):
    """A nonsense Age on a subject with no scan cannot block the run."""
    _write_imaging(tmp_path, ["sub001"])
    _write_participants(tmp_path, [
        {"MRID": "sub001", "DX": "CN", "Age": "70", "Sex": "M"},
        {"MRID": "noimage", "DX": "CN", "Age": "not-a-number", "Sex": "F"},
    ])

    requires = [
        "needs_T1",
        {"csv_has_columns": ["MRID", {"name": "Age", "type": "int", "min": 0, "max": 120}]},
    ]
    report = readiness_service.check_readiness(tmp_path, "age_schema", requires)

    age = [c for c in report.csv.required_columns if c.column == "Age"][0]
    assert age.subjects_invalid == []
    assert report.csv.satisfied


def test_subject_count_counts_only_the_subjects_that_would_run(tmp_path):
    """35 T1s with 20 matching FLAIRs does not meet a 30-subject minimum, though
    it did while this counted across every modality directory."""
    mrids = [f"sub{i:03d}" for i in range(35)]
    _write_modality(tmp_path, "t1", mrids)
    _write_modality(tmp_path, "fl", mrids[:20])
    _write_participants(
        tmp_path, [{"MRID": m, "DX": "CN", "Age": "70", "Sex": "M"} for m in mrids]
    )

    report = readiness_service.check_readiness(
        tmp_path,
        "run_nichart_dlwmls_v2_harmonized",
        ["needs_T1", "needs_FLAIR", {"min_subjects": {"required": 30, "recommended": 30}}],
    )

    assert report.subject_count.actual == 20
    assert not report.subject_count.satisfied
    assert not report.satisfied


def test_subject_count_ignores_modalities_the_pipeline_does_not_require(tmp_path):
    _write_modality(tmp_path, "t1", [f"sub{i:03d}" for i in range(5)])
    _write_modality(tmp_path, "pet", [f"pet{i:03d}" for i in range(40)])

    report = readiness_service.check_readiness(
        tmp_path, "run_dlmuse_harmonized", _requires(required=30, recommended=30)
    )

    assert report.subject_count.actual == 5
    assert not report.satisfied


# ── family invariant: harmonization needs controls in every batch ────────────

def test_every_harmonized_pipeline_requires_30_cn_controls_per_batch():
    """ComBat estimates its parameters from CN subjects, separately per batch, so
    a harmonized pipeline that does not demand them will run and return numbers
    that are quietly unreliable. Guards the whole family rather than the four
    that exist today."""
    import yaml

    from app.config import Settings
    from app.services.catalog_service import parse_min_subjects

    checked = []
    for path in sorted(Settings().pipelines_path.glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        if not (data.get("harmonized") or "harmonized" in (data.get("categories") or [])):
            continue
        pid = path.stem
        checked.append(pid)

        spec = parse_min_subjects(data.get("requires") or [])
        assert spec is not None, f"{pid} declares no min_subjects"
        assert spec.required == 30, f"{pid} requires {spec.required} subjects, not 30"

        cn = [
            c for c in spec.constraints
            if any(p.column.lower() == "dx" and (p.equals or "").upper() == "CN"
                   for p in c.where)
        ]
        assert cn, f"{pid} declares no cognitively-normal constraint"
        assert len(cn) == 1, f"{pid} declares {len(cn)} CN constraints"
        assert cn[0].per == "Batch", f"{pid} CN constraint is not per Batch"
        assert cn[0].required == 30, f"{pid} needs {cn[0].required} controls per batch"
        assert cn[0].recommended == 30

        # The constraint reads DX, so the pipeline has to require that column:
        # otherwise readiness reports the cohort as unevaluable without ever
        # telling the user which column to add.
        required_cols = {
            (c if isinstance(c, str) else c.get("name", "")).lower()
            for item in (data.get("requires") or []) if isinstance(item, dict)
            for c in (item.get("csv_has_columns") or [])
        }
        assert "dx" in required_cols, f"{pid} uses DX in a constraint but does not require it"

    assert len(checked) >= 4, f"expected the harmonized family, found {checked}"
