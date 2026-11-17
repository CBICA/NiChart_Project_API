"""
PipelineResultSummary.has_outputs: the results list includes every catalog pipeline,
so has_outputs tells which ones have actually produced files in the project.
"""

import textwrap
from pathlib import Path

from app.services import results_service


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    pipelines = tmp_path / "pipelines"
    resources = tmp_path / "resources"
    project = tmp_path / "project"
    resources.mkdir(parents=True, exist_ok=True)
    _write(
        pipelines / "seg_pipe.yaml",
        """
        pipeline_name: Seg Pipe
        steps: []
        results:
          batch_features:
            file: "seg_vol/volumes.csv"
            mrid_column: "MRID"
          per_subject:
            - id: "segmentation"
              pattern: "seg/{MRID}_seg.nii.gz"
              type: "segmentation_nifti"
        """,
    )
    _write(project / "participants" / "participants.csv", "MRID,DX\nsub01,CN\nsub02,CN\n")
    return project, resources, pipelines


def _summary(project: Path, resources: Path, pipelines: Path):
    [s] = [s for s in results_service.list_pipeline_results(project, resources, pipelines)
           if s.pipeline_id == "seg_pipe"]
    return s


def test_nothing_produced(tmp_path):
    project, resources, pipelines = _setup(tmp_path)
    s = _summary(project, resources, pipelines)
    assert not s.has_batch_features and not s.has_outputs


def test_batch_csv_counts(tmp_path):
    project, resources, pipelines = _setup(tmp_path)
    _write(project / "seg_vol" / "volumes.csv", "MRID,vol\nsub01,1\n")
    assert _summary(project, resources, pipelines).has_outputs


def test_single_per_subject_file_counts(tmp_path):
    project, resources, pipelines = _setup(tmp_path)
    (project / "seg").mkdir()
    (project / "seg" / "sub02_seg.nii.gz").write_bytes(b"")
    s = _summary(project, resources, pipelines)
    assert not s.has_batch_features and s.has_outputs


# ── Only a pipeline's own results count ──────────────────────────────────────

def _setup_variants(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A plain pipeline and a harmonized variant that reruns its step, then harmonizes:
    the variant also declares the plain step's segmentations as its per-subject output."""
    project, resources, pipelines = _setup(tmp_path)
    seg_step = (
        "  - id: seg\n"
        "    tool: seg_tool\n"
        "    inputs:\n"
        "      t1: ${STUDY}/t1\n"
        "    outputs:\n"
        "      seg: ${STUDY}/seg\n"
        "      vol: ${STUDY}/seg_vol\n"
    )
    per_subject = (
        "  per_subject:\n"
        "    - id: segmentation\n"
        "      pattern: \"seg/{MRID}_seg.nii.gz\"\n"
        "      type: segmentation_nifti\n"
    )
    (pipelines / "plain.yaml").write_text(
        "pipeline_name: Plain\nharmonized: false\nsteps:\n" + seg_step
        + "results:\n  batch_features:\n    file: seg_vol/volumes.csv\n" + per_subject
    )
    (pipelines / "plain_harmonized.yaml").write_text(
        "pipeline_name: Plain (Harmonized)\nharmonized: true\nsteps:\n" + seg_step
        + "  - id: harmonize\n"
          "    tool: harmonize_tool\n"
          "    inputs:\n"
          "      vol: ${seg.outputs.vol}/volumes.csv\n"
          "    outputs:\n"
          "      harmonized: ${STUDY}/seg_vol_harmonized\n"
          "  - id: qc\n"
          "    tool: qc_tool\n"
          "    inputs: {}\n"
          "    outputs:\n"
          "      report: ${STUDY}/qc\n"
        + "results:\n  batch_features:\n    file: seg_vol_harmonized/volumes.csv\n" + per_subject
    )
    return project, resources, pipelines


def _summaries(project: Path, resources: Path, pipelines: Path) -> dict:
    return {s.pipeline_id: s for s in results_service.list_pipeline_results(project, resources, pipelines)}


def test_shared_earlier_output_does_not_make_the_variant_present(tmp_path):
    project, resources, pipelines = _setup_variants(tmp_path)
    _write(project / "seg_vol" / "volumes.csv", "MRID,vol\nsub01,1\n")
    _write(project / "seg" / "sub01_seg.nii.gz", "")
    s = _summaries(project, resources, pipelines)
    assert s["plain"].has_outputs
    assert not s["plain_harmonized"].has_outputs


def test_variant_is_present_once_its_final_step_has_written(tmp_path):
    """The final result-producing step is 'harmonize' — not 'qc', the last step,
    which writes no declared result."""
    project, resources, pipelines = _setup_variants(tmp_path)
    _write(project / "seg" / "sub01_seg.nii.gz", "")
    _write(project / "seg_vol_harmonized" / "volumes.csv", "MRID,vol\nsub01,1\n")
    assert _summaries(project, resources, pipelines)["plain_harmonized"].has_outputs


def test_earlier_output_is_labelled_with_the_pipeline_it_belongs_to(tmp_path):
    project, resources, pipelines = _setup_variants(tmp_path)
    _write(project / "seg" / "sub01_seg.nii.gz", "")
    detail = results_service.get_pipeline_result_detail(
        project, resources, pipelines, "plain_harmonized", "Plain (Harmonized)",
    )
    [seg] = detail.per_subject
    assert seg.from_earlier_step and seg.produced_by_step == "seg"
    assert seg.same_as_pipeline_id == "plain"
    assert seg.origin_note == "From Plain — not harmonized"

    plain = results_service.get_pipeline_result_detail(project, resources, pipelines, "plain", "Plain")
    assert not plain.per_subject[0].from_earlier_step and plain.per_subject[0].origin_note is None


def test_catalog_final_steps():
    from app.config import Settings
    from app.services.catalog_service import get_result_origins

    pipelines = Settings().pipelines_path
    harmonized = get_result_origins(pipelines, "run_dlmuse_harmonized")
    assert harmonized.final_step == "harmonize_dlmuse"
    assert harmonized.producers["segmentation"] == "run_nichart_dlmuse"
    # DLWMLS's last step merges CSVs into a non-result file; its results come from before.
    assert get_result_origins(pipelines, "run_nichart_dlwmls_v2").final_step == "run_nichart_dlwmls"
