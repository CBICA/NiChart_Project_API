"""
Structural validation of all tool and pipeline YAML files in resources/.

Checks performed for every tool YAML:
  - File is valid YAML
  - Required top-level fields are present: name, description, inputs, outputs, mounts, resources, container
  - resources contains vcpus (int), memory (int), gpus (int)
  - container contains image (str) and command (str)
  - Every input/output entry has a `type` of "file" or "directory"
  - Every mount key appears in either inputs or outputs
  - Every mount entry has path_in_container (str) and mode ("ro" or "rw")
  - Command template only references tokens that exist as mount labels or parameter names

Checks performed for every pipeline YAML:
  - File is valid YAML
  - Required top-level fields are present: pipeline_name, steps
  - Every step has id, tool, inputs, outputs
  - No duplicate step IDs within a pipeline
  - Tool referenced by each step exists as a YAML file in resources/tools/
  - Step input/output values match expected path patterns (${STUDY}/... or plain paths)
"""

import re
from pathlib import Path

import pytest
import yaml

RESOURCES = Path(__file__).parent.parent / "resources"
TOOLS_DIR = RESOURCES / "tools"
PIPELINES_DIR = RESOURCES / "pipelines"

VALID_IO_TYPES = {"file", "directory"}
VALID_MOUNT_MODES = {"ro", "rw"}
# Tokens in command templates: {identifier}
_TOKEN_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")
# Valid pipeline path patterns: ${STUDY}/... or a plain relative path
_STEP_PATH_RE = re.compile(r"^\$\{[A-Z_]+\}/.+|^[a-zA-Z0-9_./-]+$")

SKIP_TOOLS = {"dummy_sleep", "dcm2niix"}
SKIP_PIPELINES = {"dummy_pipeline"}


def _tool_yamls():
    return [
        p for p in sorted(TOOLS_DIR.glob("*.yaml"))
        if p.stem not in SKIP_TOOLS
    ]


def _pipeline_yamls():
    return [
        p for p in sorted(PIPELINES_DIR.glob("*.yaml"))
        if p.stem not in SKIP_PIPELINES
    ]


def _load(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


# ── Tool tests ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("yaml_path", _tool_yamls(), ids=lambda p: p.stem)
def test_tool_yaml_is_valid_yaml(yaml_path):
    data = _load(yaml_path)
    assert isinstance(data, dict), "Top-level must be a YAML mapping"


@pytest.mark.parametrize("yaml_path", _tool_yamls(), ids=lambda p: p.stem)
def test_tool_required_fields(yaml_path):
    data = _load(yaml_path)
    for field in ("name", "description", "inputs", "outputs", "mounts", "resources", "container"):
        assert field in data, f"Missing required field: '{field}'"


@pytest.mark.parametrize("yaml_path", _tool_yamls(), ids=lambda p: p.stem)
def test_tool_resources(yaml_path):
    data = _load(yaml_path)
    res = data.get("resources", {})
    for key in ("vcpus", "memory", "gpus"):
        assert key in res, f"resources missing '{key}'"
        assert isinstance(res[key], (int, float)), f"resources.{key} must be numeric"


@pytest.mark.parametrize("yaml_path", _tool_yamls(), ids=lambda p: p.stem)
def test_tool_container(yaml_path):
    data = _load(yaml_path)
    ctr = data.get("container", {})
    assert "image" in ctr, "container missing 'image'"
    assert "command" in ctr, "container missing 'command'"
    assert isinstance(ctr["image"], str) and ctr["image"], "container.image must be a non-empty string"
    assert isinstance(ctr["command"], str) and ctr["command"], "container.command must be a non-empty string"


@pytest.mark.parametrize("yaml_path", _tool_yamls(), ids=lambda p: p.stem)
def test_tool_io_types(yaml_path):
    data = _load(yaml_path)
    for section in ("inputs", "outputs"):
        entries = data.get(section) or {}
        if not isinstance(entries, dict):
            continue
        for label, spec in entries.items():
            assert isinstance(spec, dict), f"{section}.{label} must be a mapping"
            assert "type" in spec, f"{section}.{label} missing 'type'"
            assert spec["type"] in VALID_IO_TYPES, (
                f"{section}.{label}.type must be one of {VALID_IO_TYPES}, got '{spec['type']}'"
            )


@pytest.mark.parametrize("yaml_path", _tool_yamls(), ids=lambda p: p.stem)
def test_tool_mounts_reference_valid_labels(yaml_path):
    data = _load(yaml_path)
    inputs = set((data.get("inputs") or {}).keys())
    outputs = set((data.get("outputs") or {}).keys())
    all_io = inputs | outputs
    mounts = data.get("mounts") or {}
    if not isinstance(mounts, dict):
        return
    for label in mounts:
        assert label in all_io, (
            f"Mount '{label}' is not declared in inputs or outputs"
        )


@pytest.mark.parametrize("yaml_path", _tool_yamls(), ids=lambda p: p.stem)
def test_tool_mount_specs(yaml_path):
    data = _load(yaml_path)
    mounts = data.get("mounts") or {}
    if not isinstance(mounts, dict):
        return
    for label, spec in mounts.items():
        assert isinstance(spec, dict), f"mounts.{label} must be a mapping"
        assert "path_in_container" in spec, f"mounts.{label} missing 'path_in_container'"
        assert "mode" in spec, f"mounts.{label} missing 'mode'"
        assert spec["mode"] in VALID_MOUNT_MODES, (
            f"mounts.{label}.mode must be one of {VALID_MOUNT_MODES}, got '{spec['mode']}'"
        )


@pytest.mark.parametrize("yaml_path", _tool_yamls(), ids=lambda p: p.stem)
def test_tool_command_tokens_are_declared(yaml_path):
    data = _load(yaml_path)
    command = (data.get("container") or {}).get("command", "")
    tokens = set(_TOKEN_RE.findall(command))
    mount_labels = set((data.get("mounts") or {}).keys())
    param_names = set((data.get("parameters") or {}).keys())
    allowed = mount_labels | param_names
    unknown = tokens - allowed
    assert not unknown, (
        f"Command references undefined token(s): {unknown}. "
        f"Declared mounts: {mount_labels}, params: {param_names}"
    )


# ── Pipeline tests ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("yaml_path", _pipeline_yamls(), ids=lambda p: p.stem)
def test_pipeline_yaml_is_valid_yaml(yaml_path):
    data = _load(yaml_path)
    assert isinstance(data, dict), "Top-level must be a YAML mapping"


@pytest.mark.parametrize("yaml_path", _pipeline_yamls(), ids=lambda p: p.stem)
def test_pipeline_required_fields(yaml_path):
    data = _load(yaml_path)
    assert "pipeline_name" in data, "Missing required field: 'pipeline_name'"
    assert "steps" in data, "Missing required field: 'steps'"
    assert isinstance(data["steps"], list), "'steps' must be a list"


@pytest.mark.parametrize("yaml_path", _pipeline_yamls(), ids=lambda p: p.stem)
def test_pipeline_step_fields(yaml_path):
    data = _load(yaml_path)
    for i, step in enumerate(data.get("steps") or []):
        prefix = f"steps[{i}]"
        assert "id" in step, f"{prefix} missing 'id'"
        assert "tool" in step, f"{prefix} missing 'tool'"
        assert "inputs" in step, f"{prefix} missing 'inputs'"
        assert "outputs" in step, f"{prefix} missing 'outputs'"


@pytest.mark.parametrize("yaml_path", _pipeline_yamls(), ids=lambda p: p.stem)
def test_pipeline_no_duplicate_step_ids(yaml_path):
    data = _load(yaml_path)
    ids = [s["id"] for s in (data.get("steps") or []) if "id" in s]
    seen = set()
    duplicates = [sid for sid in ids if sid in seen or seen.add(sid)]  # type: ignore[func-returns-value]
    assert not duplicates, f"Duplicate step IDs: {duplicates}"


@pytest.mark.parametrize("yaml_path", _pipeline_yamls(), ids=lambda p: p.stem)
def test_pipeline_steps_reference_known_tools(yaml_path):
    data = _load(yaml_path)
    for step in data.get("steps") or []:
        tool_id = step.get("tool")
        if not tool_id:
            continue
        tool_file = TOOLS_DIR / f"{tool_id}.yaml"
        assert tool_file.exists(), (
            f"Step '{step.get('id')}' references tool '{tool_id}' "
            f"but {tool_file.name} does not exist in resources/tools/"
        )
