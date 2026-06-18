"""
Docker job backend — runs tool jobs as sibling containers on the host daemon (DooD).

Setup requirements
------------------
1. Mount the Docker socket into the API container::

       volumes:
         - /var/run/docker.sock:/var/run/docker.sock

2. Use a host bind-mount (not a named volume) for the data directory so that the
   sibling containers can reach the same files::

       volumes:
         - ${NICHART_HOST_DATA_PATH:-./data}:/data

3. Set ``NICHART_HOST_DATA_ROOT`` to the host-side path of the data directory
   (e.g. ``/home/user/nichart-data``) so the backend can translate container-internal
   paths to host paths when building the sibling container's volume mounts.
   When ``NICHART_HOST_DATA_ROOT`` is not set, ``NICHART_DATA_ROOT`` is used as-is
   (works when the API server runs directly on the host).
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import docker
import docker.types

from app.backends.base import JobBackend, JobHandle, ToolSpec
from app.config import Settings


class DockerJobHandle(JobHandle):
    """Wraps a Docker container object."""

    def __init__(self, container) -> None:
        self._container = container

    @property
    def job_id(self) -> str:
        return self._container.id[:12]

    async def status(self) -> str:
        await asyncio.to_thread(self._container.reload)
        s = self._container.status
        if s == "exited":
            exit_code = self._container.attrs["State"]["ExitCode"]
            return "succeeded" if exit_code == 0 else "failed"
        if s in ("running", "restarting"):
            return "running"
        return "pending"  # created, paused, etc.

    async def logs(self, tail: int = 200) -> str:
        raw = await asyncio.to_thread(lambda: self._container.logs(tail=tail))
        return raw.decode("utf-8", errors="replace")

    async def cancel(self) -> None:
        try:
            await asyncio.to_thread(self._container.stop, timeout=10)
        except Exception:
            pass


class DockerBackend(JobBackend):
    """Submits tool jobs as Docker containers via the host daemon."""

    def __init__(self, settings: Settings) -> None:
        self._client = docker.from_env()
        self._data_root = settings.data_root
        self._host_data_root = settings.host_data_root or settings.data_root

    def _host_path(self, container_path: str) -> str:
        """Translate a container-internal data path to the corresponding host-side path."""
        p = Path(container_path)
        try:
            rel = p.relative_to(self._data_root)
            return str(self._host_data_root / rel)
        except ValueError:
            return container_path  # not under data_root — use as-is

    async def submit(
        self,
        tool_spec: ToolSpec,
        mount_paths: dict[str, str],
        params: dict[str, Any],
        num_subjects: int = 1,
        user_token: str | None = None,
    ) -> DockerJobHandle:
        resolved_params = tool_spec.resolve_params(params)
        command = tool_spec.render_command(resolved_params)

        volumes: dict[str, dict] = {}
        for label, container_path_str in mount_paths.items():
            if label not in tool_spec.mounts:
                continue
            mount = tool_spec.mounts[label]
            host_path = self._host_path(container_path_str)
            Path(host_path).mkdir(parents=True, exist_ok=True)
            volumes[host_path] = {"bind": mount.path_in_container, "mode": mount.mode}

        gpus = (tool_spec.resources or {}).get("gpus", 0)
        device_requests = (
            [docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])]
            if gpus else []
        )

        name = f"nichart-{tool_spec.tool_id}-{uuid.uuid4().hex[:8]}"
        container = await asyncio.to_thread(
            self._client.containers.run,
            image=tool_spec.image,
            command=command,
            volumes=volumes or None,
            detach=True,
            remove=False,
            name=name,
            device_requests=device_requests or None,
        )
        return DockerJobHandle(container)
