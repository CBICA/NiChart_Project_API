from fastapi import Depends

from app.backends.base import JobBackend
from app.config import Settings, get_settings


def get_backend(settings: Settings = Depends(get_settings)) -> JobBackend:
    """FastAPI dependency that returns the active job backend.

    Selection order:
      1. NICHART_JOB_BACKEND (explicit override) — docker | singularity | slurm | batch
      2. Auto-detect from execution_mode + sif_dir:
           cloud                              → batch
           local + sif_dir set               → singularity
           local (default)                   → docker
    """
    return get_backend_instance(settings)


def get_backend_instance(settings: Settings) -> JobBackend:
    """Non-Depends version for startup code (e.g. SLURM run resume in lifespan)."""
    backend = settings.job_backend or _auto_backend(settings)
    return _make_backend(backend, settings)


def _make_backend(backend: str, settings: Settings) -> JobBackend:
    if backend == "batch":
        from app.backends.batch_backend import BatchBackend
        return BatchBackend(settings)
    if backend == "singularity":
        from app.backends.singularity_backend import SingularityBackend
        return SingularityBackend(settings)
    if backend == "slurm":
        from app.backends.slurm_backend import SlurmBackend
        return SlurmBackend(settings)
    if backend == "docker":
        from app.backends.docker_backend import DockerBackend
        return DockerBackend(settings)

    raise ValueError(
        f"Unknown job backend {backend!r}. "
        "Valid values: 'docker', 'singularity', 'slurm', 'batch'."
    )


def _auto_backend(settings: Settings) -> str:
    if settings.execution_mode == "cloud":
        return "batch"
    if settings.sif_dir:
        return "singularity"
    return "docker"
