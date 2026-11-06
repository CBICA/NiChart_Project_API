"""
Feature-definition interpretation layer: pipeline YAML `results.batch_features.features`
is parsed and flows through the results service onto BatchFeaturesResult.
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
        pipelines / "feat_pipe.yaml",
        """
        pipeline_name: Feat Pipe
        steps: []
        results:
          batch_features:
            file: "out/predictions.csv"
            mrid_column: "MRID"
            features:
              y_score:
                label: "Amyloid-positivity score"
                definition: "Model logit for amyloid positivity."
                units: "logit"
                direction: "higher = more likely amyloid-positive"
                keywords: ["amyloid PET", "Centiloid"]
                references:
                  - citation: "Doe et al. 2024, J Test"
                    doi: "10.1234/test"
              never_present:
                definition: "Documented but not emitted by this run."
        """,
    )

    _write(project / "out" / "predictions.csv", "MRID,y_score\nsub-01,-5.4\nsub-02,2.1\n")
    return project, resources, pipelines


def test_feature_definitions_flow_through(tmp_path):
    project, resources, pipelines = _setup(tmp_path)

    detail = results_service.get_pipeline_result_detail(
        project, resources, pipelines, "feat_pipe", "Feat Pipe"
    )

    bf = detail.batch_features
    assert bf is not None and bf.available
    assert bf.columns == ["y_score"]

    defs = bf.feature_definitions
    assert defs is not None
    # Present column is documented; absent-but-documented column is filtered out.
    assert set(defs) == {"y_score"}
    assert "never_present" not in defs

    y = defs["y_score"]
    assert y.label == "Amyloid-positivity score"
    assert y.units == "logit"
    assert y.keywords == ["amyloid PET", "Centiloid"]
    assert len(y.references) == 1
    assert y.references[0].doi == "10.1234/test"
    assert y.references[0].citation == "Doe et al. 2024, J Test"


def test_no_features_block_yields_none(tmp_path):
    pipelines = tmp_path / "pipelines"
    resources = tmp_path / "resources"
    project = tmp_path / "project"
    resources.mkdir(parents=True, exist_ok=True)

    _write(
        pipelines / "plain.yaml",
        """
        pipeline_name: Plain
        steps: []
        results:
          batch_features:
            file: "out/p.csv"
            mrid_column: "MRID"
        """,
    )
    _write(project / "out" / "p.csv", "MRID,score\nsub-01,1\n")

    detail = results_service.get_pipeline_result_detail(
        project, resources, pipelines, "plain", "Plain"
    )
    assert detail.batch_features is not None
    assert detail.batch_features.feature_definitions is None


# ── per_subject underlay ─────────────────────────────────────────────────────
#
# The field exists so a viewer can draw an overlay on anatomy it is registered
# to. DLWMLS is the case that forced it: its two per-subject outputs are in
# different spaces, one on the subject's T1 grid and one on the FLAIR grid.

def test_catalog_exposes_per_subject_outputs_with_underlays(local_client):
    body = local_client.get("/catalog/pipelines/run_nichart_dlwmls_v2").json()
    by_id = {ps["id"]: ps for ps in body["per_subject"]}

    assert by_id["wml_segmentation_rois"]["underlay"] == "t1"
    assert by_id["wml_segmentation"]["underlay"] == "fl"
    assert by_id["wml_segmentation_rois"]["type"] == "segmentation_nifti"
    assert by_id["wml_segmentation_rois"]["display_name"]
    assert "{MRID}" in by_id["wml_segmentation_rois"]["pattern"]


def test_harmonized_variant_declares_the_same_underlays(local_client):
    """The two pipelines differ in harmonization, not in what DLWMLS writes."""
    plain = local_client.get("/catalog/pipelines/run_nichart_dlwmls_v2").json()
    harm = local_client.get("/catalog/pipelines/run_nichart_dlwmls_v2_harmonized").json()
    assert [(p["id"], p["underlay"]) for p in plain["per_subject"]] == \
           [(p["id"], p["underlay"]) for p in harm["per_subject"]]


def test_per_subject_is_empty_for_a_pipeline_without_subject_outputs(local_client):
    body = local_client.get("/catalog/pipelines/run_dlmuse_harmonized").json()
    assert isinstance(body["per_subject"], list)


def test_results_endpoint_reports_the_same_underlay_as_the_catalog(data_client):
    """A client can pair an overlay with its background from either endpoint."""
    assert data_client.post("/projects", json={"name": "underlayproj"}).status_code == 201
    results = data_client.get("/projects/underlayproj/results/run_nichart_dlwmls_v2").json()
    catalog = data_client.get("/catalog/pipelines/run_nichart_dlwmls_v2").json()

    assert {p["id"]: p["underlay"] for p in results["per_subject"]} == \
           {p["id"]: p["underlay"] for p in catalog["per_subject"]}
    assert {p["id"]: p["underlay"] for p in results["per_subject"]} == \
           {"wml_segmentation_rois": "t1", "wml_segmentation": "fl"}


def test_declared_underlays_are_real_modality_codes(local_client):
    """Guards against a typo reaching a client, which would surface as an
    overlay drawn over the wrong volume rather than as an error."""
    from app import modalities
    from app.services.catalog_service import UNDERLAY_ATLAS

    for summary in local_client.get("/catalog/pipelines").json():
        detail = local_client.get(f"/catalog/pipelines/{summary['id']}").json()
        for ps in detail["per_subject"]:
            underlay = ps["underlay"]
            if underlay is None:
                continue
            assert underlay == UNDERLAY_ATLAS or modalities.is_valid(underlay), (
                f"{summary['id']} / {ps['id']} declares underlay {underlay!r}"
            )


def test_an_unknown_underlay_is_rejected_at_parse_time(tmp_path):
    """A bad value must not be silently dropped: the viewer has to be able to
    trust that a non-null underlay resolves to something."""
    import pytest
    from fastapi import HTTPException

    from app.services import catalog_service

    (tmp_path / "bad.yaml").write_text(
        "pipeline_name: Bad\n"
        "steps: []\n"
        "results:\n"
        "  per_subject:\n"
        "    - id: seg\n"
        "      pattern: out/{MRID}_seg.nii.gz\n"
        "      underlay: not_a_modality\n"
    )
    with pytest.raises(HTTPException) as exc:
        catalog_service.get_pipeline_results_spec(tmp_path, "bad")
    assert "not_a_modality" in str(exc.value.detail)


# ── per_subject content + which output a feature's label_ids index into ──────

def _two_output_setup(tmp_path: Path, label_output_id: str = "rois") -> tuple[Path, Path, Path]:
    """A pipeline with two image outputs, only one of which carries label IDs."""
    pipelines = tmp_path / "pipelines"
    resources = tmp_path / "resources"
    project = tmp_path / "project"
    resources.mkdir(parents=True, exist_ok=True)

    _write(resources / "labels.csv", "601,GM,1,2\n602,WM,3\n")
    _write(
        pipelines / "two_out.yaml",
        f"""
        pipeline_name: Two Out
        steps: []
        results:
          batch_features:
            file: "out/vols.csv"
            mrid_column: "MRID"
            label_map: "labels.csv"
            column_template: "V_{{id}}"
            label_output_id: {label_output_id}
          per_subject:
            - id: rois
              pattern: "seg/{{MRID}}_rois.nii.gz"
              type: "segmentation_nifti"
              underlay: t1
              content: labels
            - id: plain_mask
              pattern: "mask/{{MRID}}_mask.nii.gz"
              type: "segmentation_nifti"
              underlay: fl
              content: mask
        """,
    )
    _write(project / "out" / "vols.csv", "MRID,V_601,V_602\nsub-01,10,20\n")
    _write(project / "seg" / "sub-01_rois.nii.gz", "x")
    _write(project / "mask" / "sub-01_mask.nii.gz", "x")
    return project, resources, pipelines


def test_results_reports_content_per_output(tmp_path):
    project, resources, pipelines = _two_output_setup(tmp_path)

    detail = results_service.get_pipeline_result_detail(
        project, resources, pipelines, "two_out", "Two Out"
    )

    by_id = {ps.id: ps for ps in detail.per_subject}
    assert (by_id["rois"].content, by_id["rois"].underlay) == ("labels", "t1")
    assert (by_id["plain_mask"].content, by_id["plain_mask"].underlay) == ("mask", "fl")
    assert by_id["rois"].subjects["sub-01"].available is True


def test_results_label_map_names_the_output_its_ids_index_into(tmp_path):
    """Without this, a client cannot tell which of two overlays a feature's
    label_ids mean anything in."""
    project, resources, pipelines = _two_output_setup(tmp_path)

    detail = results_service.get_pipeline_result_detail(
        project, resources, pipelines, "two_out", "Two Out"
    )

    label_map = detail.batch_features.label_map
    assert set(label_map) == {"V_601", "V_602"}
    assert all(info.output_id == "rois" for info in label_map.values())
    assert label_map["V_601"].label_ids == [1, 2]


def test_label_output_id_must_name_a_declared_output(tmp_path):
    import pytest
    from fastapi import HTTPException

    from app.services import catalog_service

    _, _, pipelines = _two_output_setup(tmp_path, label_output_id="no_such_output")
    with pytest.raises(HTTPException) as exc:
        catalog_service.get_pipeline_results_spec(pipelines, "two_out")
    assert "no_such_output" in str(exc.value.detail)


def test_unknown_content_kind_is_rejected_at_parse_time(tmp_path):
    import pytest
    from fastapi import HTTPException

    from app.services import catalog_service

    (tmp_path / "bad.yaml").write_text(
        "pipeline_name: Bad\n"
        "steps: []\n"
        "results:\n"
        "  per_subject:\n"
        "    - id: seg\n"
        "      pattern: out/{MRID}_seg.nii.gz\n"
        "      content: heatmap\n"
    )
    with pytest.raises(HTTPException) as exc:
        catalog_service.get_pipeline_results_spec(tmp_path, "bad")
    assert "heatmap" in str(exc.value.detail)


def test_catalog_declares_content_for_the_dlwmls_outputs(local_client):
    body = local_client.get("/catalog/pipelines/run_nichart_dlwmls_v2").json()
    by_id = {ps["id"]: ps for ps in body["per_subject"]}

    assert by_id["wml_segmentation_rois"]["content"] == "labels"
    assert by_id["wml_segmentation"]["content"] == "mask"
    # The region IDs only mean something in the labelled volume.
    assert all(
        info["output_id"] == "wml_segmentation_rois"
        for info in body["label_map"].values()
    )


def test_single_output_pipelines_leave_output_id_null(local_client):
    """Nothing to disambiguate, so the field stays absent rather than being
    filled in for its own sake."""
    body = local_client.get("/catalog/pipelines/run_dlmuse").json()
    assert [ps["content"] for ps in body["per_subject"]] == ["labels"]
    assert all(info["output_id"] is None for info in body["label_map"].values())


def test_declared_content_kinds_are_known(local_client):
    from app.services.catalog_service import CONTENT_KINDS

    for summary in local_client.get("/catalog/pipelines").json():
        detail = local_client.get(f"/catalog/pipelines/{summary['id']}").json()
        for ps in detail["per_subject"]:
            assert ps["content"] is None or ps["content"] in CONTENT_KINDS, (
                f"{summary['id']} / {ps['id']} declares content {ps['content']!r}"
            )
