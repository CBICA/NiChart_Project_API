"""
Shell commands that rerun a recorded pipeline step by hand.

Given an execution record (see ``step_ledger``), print the ``docker run`` or
``apptainer`` invocation the server's backends would make — same image, mounts,
modes, GPU flag and command — with host paths rewritten relative to
``$NICHART_PROJECT``, the root of the user's copy of the project.

Kept in step with ``DockerBackend.submit`` and
``SingularityBackend._build_apptainer_argv``; differences are deliberate:
``--rm`` (nothing needs the stopped container), no ``--name``, and steps the
server split into subject chunks run as one container over the whole cohort,
which is what the chunks add up to.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from app.backends.singularity_backend import _docker_tag_to_sif_name, effective_run_mode

PROJECT_VAR = "NICHART_PROJECT"
BACKENDS = ("docker", "singularity")
CONTINUATION = " \\\n    "


def _dq(text: str) -> str:
    """Escape for the inside of a double-quoted shell string."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def host_path(rel: str) -> str:
    """A project path inside double quotes, so ``$NICHART_PROJECT`` still expands."""
    if PurePosixPath(rel).is_absolute():
        return f'"{_dq(rel)}"'
    return f'"${{{PROJECT_VAR}}}/{_dq(rel)}"'


def _host_and_bind(rel: str, container: str, mode: str) -> str:
    base = f"${{{PROJECT_VAR}}}/" if not PurePosixPath(rel).is_absolute() else ""
    return f'"{base}{_dq(rel)}:{_dq(container)}:{mode}"'


def image_ref(image: str, digest: str | None) -> str:
    """``repo:tag@sha256:...`` when the digest is known (pins the exact image), else the tag."""
    if digest and "@" in digest:
        return f"{image}@{digest.split('@', 1)[1]}"
    return image


def _binds(rec: dict) -> tuple[list[tuple[str, str, str]], list[str]]:
    """(host_rel, container_path, mode) per mount, and the output dirs to create first."""
    binds: list[tuple[str, str, str]] = []
    mkdirs: list[str] = []
    paths = {**(rec.get("inputs") or {}), **(rec.get("outputs") or {})}
    outputs = rec.get("outputs") or {}
    for label, rel in paths.items():
        m = (rec.get("mounts") or {}).get(label)
        if not m:
            continue
        container, mode, kind = m["container_path"], m.get("mode", "ro"), m.get("mount_type", "directory")
        if kind == "output_file":
            # As the backends do: mount the parent, so the file needn't exist yet.
            host_dir = str(PurePosixPath(rel).parent)
            binds.append((host_dir, str(PurePosixPath(container).parent), mode))
            mkdirs.append(host_dir)
        else:
            binds.append((rel, container, mode))
            if label in outputs and kind == "directory":
                mkdirs.append(rel)
    seen: set[tuple[str, str, str]] = set()
    unique = [b for b in binds if not (b in seen or seen.add(b))]
    return unique, list(dict.fromkeys(mkdirs))


def _command_tokens(rec: dict) -> list[str]:
    return [shlex.quote(t) for t in shlex.split(rec.get("command") or "")]


def docker_command(rec: dict) -> str:
    binds, mkdirs = _binds(rec)
    lines = [f"mkdir -p {host_path(d)}" for d in mkdirs]
    head = 'docker run --rm --ipc=host --user "$(id -u):$(id -g)"'
    if rec.get("gpus"):
        head += " --gpus all"
    parts = [head]
    parts += [f"-v {_host_and_bind(h, c, mode)}" for h, c, mode in binds]
    parts.append(shlex.quote(image_ref(rec["image"], rec.get("image_digest"))))
    tokens = _command_tokens(rec)
    if tokens:
        parts.append(" ".join(tokens))
    lines.append(CONTINUATION.join(parts))
    return "\n".join(lines)


def sif_file(image: str) -> str:
    return _docker_tag_to_sif_name(image)


def singularity_command(rec: dict) -> str:
    binds, mkdirs = _binds(rec)
    lines = [f"mkdir -p {host_path(d)}" for d in mkdirs]
    mode = effective_run_mode(rec.get("singularity_run_mode"), rec.get("command") or "")
    head = f"apptainer {mode} --writable-tmpfs"
    if rec.get("gpus"):
        head += " --nv"
    parts = [head]
    # Singularity only knows ro/rw; the backend maps anything but "ro" to "rw".
    parts += [f"--bind {_host_and_bind(h, c, 'ro' if m == 'ro' else 'rw')}" for h, c, m in binds]
    parts.append(shlex.quote(sif_file(rec["image"])))
    tokens = _command_tokens(rec)
    if tokens:
        parts.append(" ".join(tokens))
    lines.append(CONTINUATION.join(parts))
    return "\n".join(lines)


def commands(rec: dict) -> dict[str, str]:
    return {"docker": docker_command(rec), "singularity": singularity_command(rec)}


def setup_commands(images: list[tuple[str, str | None]]) -> dict[str, str]:
    """Image pulls, once per image, for each backend."""
    docker = [f"docker pull {shlex.quote(image_ref(i, d))}" for i, d in images]
    sing = [
        f"apptainer pull {shlex.quote(sif_file(i))} {shlex.quote('docker://' + image_ref(i, d))}"
        for i, d in images
    ]
    return {"docker": "\n".join(docker), "singularity": "\n".join(sing)}
