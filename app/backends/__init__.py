from fastapi import Depends

from app.backends.base import JobBackend
from app.config import Settings, get_settings


def get_backend(settings: Settings = Depends(get_settings)) -> JobBackend:
    """FastAPI dependency that returns the active job backend for the configured execution mode."""
    if settings.execution_mode == "local":
        from app.backends.docker_backend import DockerBackend
        return DockerBackend(settings)
    from app.backends.batch_backend import BatchBackend
    return BatchBackend(settings)
