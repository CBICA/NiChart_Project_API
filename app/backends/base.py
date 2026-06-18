"""
Abstract job-backend interface, plus the ToolSpec dataclass that both backends consume.

Concrete implementations live in ``docker_backend.py`` (local Docker) and
``batch_backend.py`` (AWS Batch via Lambda). All pipeline orchestration code
depends only on these abstractions.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ── Tool specification ────────────────────────────────────────────────────────

@dataclass
class MountSpec:
    """One mount entry from a tool YAML's ``mounts`` section."""
    path_in_container: str
    mode: str = "ro"


@dataclass
class ToolSpec:
    """
    Complete internal representation of a tool YAML, consumed by backends.

    Distinct from ``ToolDetail`` (the API response model) — it carries the
    container image, command template, and mount configuration that only the
    execution layer needs.
    """
    tool_id: str
    name: str
    image: str
    command_template: str
    mounts: dict[str, MountSpec] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)  # raw spec dicts from YAML
    resources: dict[str, Any] = field(default_factory=dict)   # vcpus, memory, gpus
    time_per_subject_seconds: float | None = None

    def render_command(self, params: dict[str, Any]) -> str:
        """Substitute mount container paths and resolved params into the command template."""
        cmd = self.command_template
        for label, mount in self.mounts.items():
            cmd = cmd.replace(f"{{{label}}}", mount.path_in_container)
        for key, val in params.items():
            cmd = cmd.replace(f"{{{key}}}", str(val))
        return cmd

    def resolve_params(self, user_params: dict[str, Any]) -> dict[str, Any]:
        """Merge user-supplied params with YAML defaults. Raises ValueError for missing required params."""
        result: dict[str, Any] = {}
        for key, spec in self.parameters.items():
            val = user_params.get(key, spec.get("default"))
            if val is None and "default" not in spec:
                raise ValueError(f"Missing required parameter: '{key}'")
            result[key] = val
        return result


# ── Job handle ────────────────────────────────────────────────────────────────

class JobHandle(ABC):
    """Reference to a running or completed job on a specific backend."""

    @abstractmethod
    async def status(self) -> str:
        """
        Return the current job status as a normalised lowercase string.

        Common values: ``"running"``, ``"succeeded"``, ``"failed"``,
        ``"pending"``, ``"cancelled"``.
        """

    @abstractmethod
    async def logs(self, tail: int = 200) -> str:
        """Return the most recent *tail* lines of job output as a single string."""

    @abstractmethod
    async def cancel(self) -> None:
        """Request cancellation of the job. No-op if already terminal."""

    @property
    @abstractmethod
    def job_id(self) -> str:
        """Stable identifier for this job (container name or AWS Batch job ID)."""


# ── Backend ───────────────────────────────────────────────────────────────────

class JobBackend(ABC):
    """
    Abstraction over a job execution environment.

    Exactly one backend is instantiated per server process, chosen by
    ``NICHART_EXECUTION_MODE``.
    """

    @abstractmethod
    async def submit(
        self,
        tool_spec: ToolSpec,
        mount_paths: dict[str, str],
        params: dict[str, Any],
        num_subjects: int = 1,
        user_token: str | None = None,
    ) -> JobHandle:
        """
        Submit a containerised tool job and return a handle immediately.

        Parameters
        ----------
        tool_spec:
            Loaded tool specification (image, command template, mounts, etc.).
        mount_paths:
            Mapping of mount label → absolute host-side path. Labels must match
            the keys in ``tool_spec.mounts``.
        params:
            Parameter values for this invocation (merged from pipeline and user overrides).
        num_subjects:
            Hint for cloud queue-drain estimation; passed to the Lambda as ``num_subjects``.
        user_token:
            Cognito ID token forwarded to the Lambda (cloud mode only).
        """
