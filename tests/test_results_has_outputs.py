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
