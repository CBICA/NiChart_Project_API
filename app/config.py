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

    staging_ttl_hours: int = Field(
        default=24,
        description="Hours after which uncommitted staging uploads are eligible for cleanup.",
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
