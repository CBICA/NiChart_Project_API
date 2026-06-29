"""
Documentation topics — public, no authentication required.

Serves structured documentation for NiChart pipelines and tools, targeted at
both end users and developers. Content lives in resources/docs/<topic_id>/.
"""

import mimetypes

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.auth.dependencies import public
from app.config import Settings, get_settings
from app.models.docs import DocManifest, DocTopicSummary
from app.models.errors import ErrorDetail
from app.services import docs_service

router = APIRouter(
    prefix="/catalog/docs",
    tags=["Docs"],
    dependencies=[Depends(public)],
)

_NOT_FOUND = {404: {"model": ErrorDetail, "description": "Topic or file not found."}}
_BAD_PATH = {400: {"model": ErrorDetail, "description": "Invalid path."}}


@router.get(
    "",
    summary="List documentation topics",
    description=(
        "Returns a summary of every documentation topic in resources/docs/. "
        "Each topic may cover one or more related pipelines (e.g. harmonized and "
        "base DLMUSE variants share a single 'dlmuse' topic). "
        "No authentication required."
    ),
    response_model=list[DocTopicSummary],
    responses={},
)
async def list_docs(
    settings: Settings = Depends(get_settings),
) -> list[DocTopicSummary]:
    return docs_service.list_doc_topics(settings.docs_path)


@router.get(
    "/{docs_id}",
    summary="Get documentation topic manifest",
    description=(
        "Returns the manifest for a documentation topic: its sections, audience tags, "
        "and the pipeline IDs it covers. Use the ``file`` field of each section with "
        "``GET /catalog/docs/{docs_id}/{file}`` to fetch the actual content. "
        "No authentication required."
    ),
    response_model=DocManifest,
    responses={**_NOT_FOUND, **_BAD_PATH},
)
async def get_docs_manifest(
    docs_id: str,
    settings: Settings = Depends(get_settings),
) -> DocManifest:
    return docs_service.get_doc_manifest(settings.docs_path, docs_id)


@router.get(
    "/{docs_id}/{path:path}",
    summary="Fetch a documentation file",
    description=(
        "Serves any file within the documentation topic folder: markdown prose, "
        "images, or JSON data. Relative image references in markdown files resolve "
        "naturally to further requests under this same path prefix. "
        "Path traversal outside the topic folder is rejected. "
        "No authentication required."
    ),
    response_class=FileResponse,
    responses={
        200: {"content": {"application/octet-stream": {}}},
        **_NOT_FOUND,
        **_BAD_PATH,
    },
)
async def get_docs_file(
    docs_id: str,
    path: str,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    resolved = docs_service.resolve_doc_file(settings.docs_path, docs_id, path)
    media_type, _ = mimetypes.guess_type(str(resolved))
    return FileResponse(
        path=resolved,
        media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )
