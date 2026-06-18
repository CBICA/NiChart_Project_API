"""
Pipeline and tool catalog — public, no authentication required.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.auth.dependencies import public
from app.config import Settings, get_settings
from app.models.catalog import PipelineDetail, PipelineSummary, ToolDetail, ToolSummary
from app.models.errors import ErrorDetail
from app.services import catalog_service
from app.services.path_security import PathEscapeError, assert_safe_path

router = APIRouter(prefix="/catalog", tags=["Catalog"], dependencies=[Depends(public)])

_COMMON_ERRORS = {
    404: {"model": ErrorDetail, "description": "Resource not found."},
}


@router.get(
    "/pipelines",
    summary="List all enabled pipelines",
    description=(
        "Returns a summary of every pipeline defined in the resources/pipelines directory. "
        "Use ``GET /catalog/pipelines/{pipeline_id}`` to retrieve the full definition."
    ),
    response_model=list[PipelineSummary],
    responses={},
)
async def list_pipelines(settings: Settings = Depends(get_settings)) -> list[PipelineSummary]:
    return catalog_service.list_pipelines(settings.pipelines_path)


@router.get(
    "/pipelines/{pipeline_id}",
    summary="Get pipeline detail",
    description=(
        "Returns the full pipeline definition: description, categories, data "
        "requirements, and ordered steps with their tool and I/O mappings."
    ),
    response_model=PipelineDetail,
    responses=_COMMON_ERRORS,
)
async def get_pipeline(
    pipeline_id: str,
    settings: Settings = Depends(get_settings),
) -> PipelineDetail:
    return catalog_service.get_pipeline(settings.pipelines_path, pipeline_id)


@router.get(
    "/tools",
    summary="List all available tools",
    description="Returns a summary of every tool defined in the resources/tools directory.",
    response_model=list[ToolSummary],
    responses={},
)
async def list_tools(settings: Settings = Depends(get_settings)) -> list[ToolSummary]:
    return catalog_service.list_tools(settings.tools_path)


@router.get(
    "/tools/{tool_id}",
    summary="Get tool detail",
    description=(
        "Returns the full tool specification: inputs, outputs, resource requirements, "
        "configurable parameters, and the per-subject time estimate used for queue-drain "
        "calculations."
    ),
    response_model=ToolDetail,
    responses=_COMMON_ERRORS,
)
async def get_tool(
    tool_id: str,
    settings: Settings = Depends(get_settings),
) -> ToolDetail:
    return catalog_service.get_tool(settings.tools_path, tool_id)


@router.get(
    "/resources/{path:path}",
    summary="Download a public resource file",
    description=(
        "Serves static resource files from the server's ``resources/`` directory: "
        "atlases, label maps, normative data CSVs, and other reference data. "
        "No authentication required. Responses are cached for 24 hours."
    ),
    response_class=FileResponse,
    responses={
        200: {"content": {"application/octet-stream": {}}},
        400: {"model": ErrorDetail, "description": "Invalid resource path."},
        404: {"model": ErrorDetail, "description": "Resource not found."},
    },
)
async def get_resource(
    path: str,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    try:
        resource_path = (settings.resources_path / path).resolve()
        assert_safe_path(settings.resources_path, resource_path)
    except PathEscapeError:
        raise HTTPException(400, "Invalid resource path.")
    except Exception:
        raise HTTPException(400, "Invalid resource path.")

    if not resource_path.exists() or not resource_path.is_file():
        raise HTTPException(404, f"Resource '{path}' not found.")

    return FileResponse(
        path=resource_path,
        headers={"Cache-Control": "public, max-age=86400"},
    )
