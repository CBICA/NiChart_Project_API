from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All runtime configuration for the NiChart API.

    Every field is read from environment variables prefixed with ``NICHART_``.
    A ``.env`` file in the working directory is loaded automatically.
    """

    model_config = SettingsConfigDict(
        env_prefix="NICHART_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    execution_mode: Literal["cloud", "local"] = Field(
        default="local",
        description="Job execution backend. 'local' uses Docker; 'cloud' uses AWS Batch via Lambda.",
    )
    data_root: Path = Field(
        default=Path("/data"),
        description="Root directory under which all user project data is stored.",
    )
    resources_path: Path = Field(
        default=Path("resources"),
        description="Directory containing pipeline and tool YAML definitions.",
    )

    # AWS Cognito — only used when execution_mode == "cloud"
    cognito_region: str = Field(default="us-east-1")
    cognito_user_pool_id: str = Field(default="us-east-1_BSBhcKA66")
    cognito_identity_pool_id: str = Field(
        default="us-east-1:12c87a16-8336-450c-bf25-b98990c7dcf8"
    )
    cognito_client_id: str = Field(
        default="1ugglpalgp9r2gvb24s2v7dunq",
        description=(
            "Cognito App Client ID. ID tokens must be issued for this client "
            "(audience claim check). Public SPA client — no secret."
        ),
    )

    # AWS Batch / Lambda — only used when execution_mode == "cloud"
    batch_queue_name: str = Field(
        default="cbica-nichart-jobqueue-standard",
        description="Name of the AWS Batch job queue used for pipeline jobs.",
    )
    lambda_function_name: str = Field(
        default="cbica-nichart-submitjob",
        description="Name of the Lambda function used to submit Batch jobs.",
    )

    cors_origins: list[str] = Field(
        default=["http://localhost:3000"],
        description=(
            "List of allowed CORS origins. In production set this to the UI's domain. "
            "Use [\"*\"] only for fully public, unauthenticated APIs."
        ),
    )

    # S3 data sync — cloud mode only.
    # When s3_data_bucket is set the job orchestrator syncs project data to S3
    # before submitting each step and pulls results back after completion.
    # This removes the FSx dependency for cloud-local testing and makes FSx
    # optional in production (FSx remains useful for concurrent jobs at scale).
    s3_data_bucket: str | None = Field(
        default=None,
        description=(
            "S3 bucket for project data sync. When set, data is uploaded before "
            "each pipeline step and downloaded after. e.g. 'cbica-nichart-io'."
        ),
    )
    s3_data_prefix: str = Field(
        default="fsx",
        description="Key prefix within s3_data_bucket mirroring the FSx layout.",
    )

    staging_ttl_hours: int = Field(
        default=24,
        description="Hours after which uncommitted staging uploads are eligible for cleanup.",
    )

    # Explicit backend override.
    # When set, this takes precedence over the execution_mode auto-selection.
    # Valid values: "docker", "singularity", "batch".
    # When unset (default), the backend is chosen automatically:
    #   execution_mode=cloud                         → batch
    #   execution_mode=local + sif_dir is set        → singularity
    #   execution_mode=local (default)               → docker
    job_backend: str | None = Field(
        default=None,
        description=(
            "Explicit job backend override: 'docker', 'singularity', or 'batch'. "
            "When unset, the backend is inferred from execution_mode and sif_dir."
        ),
    )

    # Singularity/Apptainer local mode.
    # When sif_dir is set AND execution_mode is 'local', the Singularity backend is
    # used instead of Docker. Run scripts/build-sif-registry.py first to populate it.
    sif_dir: Path | None = Field(
        default=None,
        description=(
            "Directory containing pre-built .sif images. "
            "When set in local mode, the Singularity backend is used instead of Docker."
        ),
    )
    container_runner: str = Field(
        default="apptainer",
        description="Singularity runner command ('apptainer' or 'singularity').",
    )

    # Docker (local mode) — host-side path to the data directory so sibling containers
    # can mount the same directory via DooD (Docker outside Docker).
    # When None, data_root is used as-is (works when running outside Docker).
    host_data_root: Path | None = Field(
        default=None,
        description=(
            "Host-side path to the data root, used by the Docker backend to mount "
            "the data directory into sibling containers. Required when the API server "
            "itself runs inside Docker and spawns sibling containers (DooD). "
            "Defaults to data_root when not set."
        ),
    )

    # SLURM backend — only used when NICHART_JOB_BACKEND=slurm.
    # Requires apptainer on the compute nodes and NICHART_SIF_DIR set.
    slurm_partition: str | None = Field(
        default=None,
        description="SLURM partition for job submissions (e.g. 'gpu'). Uses cluster default if unset.",
    )
    slurm_account: str | None = Field(
        default=None,
        description="SLURM account/project to charge (--account flag). Uses user default if unset.",
    )
    slurm_extra_sbatch_args: list[str] = Field(
        default=[],
        description="Extra sbatch flags appended verbatim, e.g. ['--constraint=h100'].",
    )
    slurm_logs_dir: Path | None = Field(
        default=None,
        description=(
            "Directory for SLURM job log files. Named {job_id}.log. "
            "Defaults to <data_root>/_slurm_logs. "
            "Must be on a filesystem accessible to both the API server and compute nodes."
        ),
    )
    slurm_time_safety_factor: float = Field(
        default=2.0,
        description="Multiply estimated job time by this factor when setting --time.",
    )
    slurm_default_time_minutes: int = Field(
        default=1440,
        description="SLURM --time limit (minutes) when no time_per_subject_seconds estimate is available.",
    )
    slurm_sbatch_cmd: str = Field(default="sbatch", description="Path or name of the sbatch executable.")
    slurm_squeue_cmd: str = Field(default="squeue", description="Path or name of the squeue executable.")
    slurm_sacct_cmd: str = Field(default="sacct", description="Path or name of the sacct executable.")
    slurm_scancel_cmd: str = Field(default="scancel", description="Path or name of the scancel executable.")

    @property
    def jwks_url(self) -> str:
        return (
            f"https://cognito-idp.{self.cognito_region}.amazonaws.com"
            f"/{self.cognito_user_pool_id}/.well-known/jwks.json"
        )

    @property
    def cognito_issuer(self) -> str:
        return (
            f"https://cognito-idp.{self.cognito_region}.amazonaws.com"
            f"/{self.cognito_user_pool_id}"
        )

    @property
    def pipelines_path(self) -> Path:
        return self.resources_path / "pipelines"

    @property
    def tools_path(self) -> Path:
        return self.resources_path / "tools"


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()
