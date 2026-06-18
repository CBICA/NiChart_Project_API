"""
Pipeline result inspection — per-project, authenticated.

URL structure:
  GET /projects/{project_id}/results                → summaries for all pipelines with results
  GET /projects/{project_id}/results/{pipeline_id}  → full detail for one pipeline
"""

from fastapi import APIRouter, Depends

from app.auth.dependencies import CurrentUser, require_auth
from app.config import Settings, get_settings
from app.models.errors import ErrorDetail
from app.models.results import PipelineResultDetail, PipelineResultSummary
from app.services import catalog_service, file_service, results_service

router = APIRouter(
    prefix="/projects/{project_id}/results",
    tags=["Results"],
)

_AUTH_ERRORS = {
    401: {"model": ErrorDetail, "description": "Missing or invalid token."},
    403: {"model": ErrorDetail, "description": "Access denied."},
}


@router.get(
    "",
    summary="List available pipeline results",
    description=(
        "Returns a summary for every pipeline that declares a ``results:`` section "
        "in its YAML. Summaries indicate which output types are present in the project. "
        "Use this to discover which pipelines have results to visualize — one call "
        "covers all pipelines."
    ),
    response_model=list[PipelineResultSummary],
    responses={
        **_AUTH_ERRORS,
        404: {"model": ErrorDetail, "description": "Project not found."},
    },
)
async def list_results(
    project_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> list[PipelineResultSummary]:
    pdir = file_service.resolve_project(settings, user, project_id)
    return results_service.list_pipeline_results(
        project_path=pdir,
        resources_path=settings.resources_path,
        pipelines_path=settings.pipelines_path,
    )


@router.get(
    "/{pipeline_id}",
    summary="Get full result detail for a pipeline",
    description=(
        "Returns the complete result structure for one pipeline: "
        "feature columns, label map (column → segmentation label IDs for overlay rendering), "
        "per-subject file availability, subject completeness, "
        "and resource paths for the atlas NIfTI and normative data CSV. "
        "\n\n"
        "**Atlas and normative data** are fetched via ``GET /catalog/resources/{path}`` "
        "(public, no auth required, cached 24 h)."
    ),
    response_model=PipelineResultDetail,
    responses={
        **_AUTH_ERRORS,
        404: {"model": ErrorDetail, "description": "Pipeline or project not found."},
    },
)
async def get_result_detail(
    project_id: str,
    pipeline_id: str,
    user: CurrentUser = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> PipelineResultDetail:
    pdir = file_service.resolve_project(settings, user, project_id)
    pipeline = catalog_service.get_pipeline(settings.pipelines_path, pipeline_id)
    return results_service.get_pipeline_result_detail(
        project_path=pdir,
        resources_path=settings.resources_path,
        pipelines_path=settings.pipelines_path,
        pipeline_id=pipeline_id,
        pipeline_name=pipeline.name,
    )
