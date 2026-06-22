"""
NiChart API — application factory.

Import ``app`` for production use::

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Call ``create_app()`` in tests to get a fresh, isolated instance.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth.dependencies import public
from app.config import Settings, get_settings
from app.routers.catalog import router as catalog_router
from app.routers.cloud import router as cloud_router
from app.routers.dicom import router as dicom_router
from app.routers.files import router as files_router
from app.routers.jobs import jobs_router, project_router as jobs_project_router
from app.routers.projects import router as projects_router
from app.routers.results import router as results_router


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup / shutdown hook. Extend here as services are added."""
    import logging

    from app.backends import get_backend_instance
    from app.services import job_service

    _log = logging.getLogger(__name__)
    settings = get_settings()
    # Ensure data root exists (local mode); cloud FSx handles this transparently.
    if settings.execution_mode == "local":
        settings.data_root.mkdir(parents=True, exist_ok=True)
    # Restore run history from disk. SLURM runs still marked "running" are left
    # as-is so resume_runs() below can reconnect to them.
    job_service.load_runs_from_disk(settings.data_root)
    # Re-attach polling tasks for any SLURM jobs that survived the last restart.
    if job_service.has_slurm_runs_to_resume():
        try:
            backend = get_backend_instance(settings)
            await job_service.resume_runs(settings, backend)
        except Exception as exc:
            _log.warning("Could not resume SLURM runs at startup: %s", exc)
    yield


def create_app() -> FastAPI:
    """
    Construct and return a configured FastAPI application instance.

    Keeping construction in a factory function (rather than module-level)
    means tests can call ``create_app()`` to get isolated instances with
    dependency overrides applied before the first request.
    """
    settings = get_settings()

    app = FastAPI(
        title="NiChart API",
        summary="Backend API for the NiChart medical-imaging pipeline platform.",
        description=(
            "Provides endpoints for managing projects, uploading imaging data, "
            "running containerised processing pipelines (locally via Docker or on "
            "AWS Batch), and retrieving results. "
            "\n\n"
            "**Authentication**: In cloud mode every request requires a Cognito ID "
            "token in the ``Authorization: Bearer`` header. In local mode "
            "(``NICHART_EXECUTION_MODE=local``) authentication is bypassed."
        ),
        version="0.1.0",
        contact={"name": "CBICA", "url": "https://github.com/CBICA/NiChart_Project"},
        license_info={"name": "MIT"},
        lifespan=_lifespan,
        # Expose docs in all modes for now; restrict in prod if desired.
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # ── CORS ─────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(catalog_router)
    app.include_router(cloud_router)
    app.include_router(projects_router)
    app.include_router(files_router)
    app.include_router(dicom_router)
    app.include_router(jobs_project_router)
    app.include_router(jobs_router)
    app.include_router(results_router)

    # ── Health ────────────────────────────────────────────────────────────────
    @app.get(
        "/health",
        tags=["Health"],
        summary="Health check",
        description="Returns server status and the active execution mode. No authentication required.",
        dependencies=[],   # explicitly public — no require_auth
        response_class=JSONResponse,
    )
    async def health(settings: Settings = Depends(get_settings)):
        return {
            "status": "ok",
            "execution_mode": settings.execution_mode,
            "version": "0.1.0",
        }

    return app


# Module-level singleton used by uvicorn / gunicorn
app = create_app()
