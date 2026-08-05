#!/usr/bin/env python3
"""
Check that file-type outputs agree on their filename between pipelines and tools.

Because of how output files are mounted (the parent directory is bind-mounted and
the tool writes to its ``path_in_container`` basename inside it), a file output only
lands where the pipeline expects it if:

    basename(tool.mounts[label].path_in_container) == basename(step.outputs[label])

Parent paths may differ freely; only the filename must match. Directory outputs
have no such constraint. A mismatch produces a silently-empty output at runtime
(the tool writes one name, the pipeline looks for another), so this guards against
that whole class of bug.

Usage:
    python scripts/check_pipeline_tool_filenames.py

Exit status:
    0  all file outputs consistent (and every output label wired to a tool)
    1  one or more mismatches / missing wiring found
    2  a resources directory or YAML could not be read
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINES_DIR = REPO_ROOT / "resources" / "pipelines"
TOOLS_DIR = REPO_ROOT / "resources" / "tools"


def _load(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def main() -> int:
    if not PIPELINES_DIR.is_dir() or not TOOLS_DIR.is_dir():
        print(f"ERROR: expected {PIPELINES_DIR} and {TOOLS_DIR} to exist", file=sys.stderr)
        return 2

    tools: dict[str, dict] = {}
    for tp in sorted(TOOLS_DIR.glob("*.yaml")):
        try:
            data = _load(tp)
        except Exception as exc:
            print(f"ERROR: could not parse tool {tp.name}: {exc}", file=sys.stderr)
            return 2
        if isinstance(data, dict):
            tools[tp.stem] = data

    mismatches: list[str] = []
    missing: list[str] = []
    checked = 0

    for pp in sorted(PIPELINES_DIR.glob("*.yaml")):
        try:
            pdef = _load(pp)
        except Exception as exc:
            print(f"ERROR: could not parse pipeline {pp.name}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(pdef, dict):
            continue

        for step in pdef.get("steps") or []:
            tool_id = step.get("tool")
            step_id = step.get("id")
            tdef = tools.get(tool_id)
            if tdef is None:
                missing.append(f"{pp.name} step '{step_id}': tool '{tool_id}' not found")
                continue

            t_outputs = tdef.get("outputs") or {}
            t_mounts = tdef.get("mounts") or {}
            for label, pipe_path in (step.get("outputs") or {}).items():
                tout = t_outputs.get(label)
                if tout is None:
                    missing.append(
                        f"{pp.name} step '{step_id}': output '{label}' not declared in tool '{tool_id}'"
                    )
                    continue
                if tout.get("type") != "file":
                    continue  # directory outputs have no filename constraint

                mount = t_mounts.get(label)
                if mount is None:
                    missing.append(
                        f"{pp.name} step '{step_id}': file output '{label}' has no mount in tool '{tool_id}'"
                    )
                    continue

                checked += 1
                pic = mount.get("path_in_container", "")
                tool_base = os.path.basename(pic)
                pipe_base = os.path.basename(str(pipe_path))
                if tool_base != pipe_base:
                    mismatches.append(
                        f"{pp.name}  step '{step_id}' (tool {tool_id}), output '{label}':\n"
                        f"      tool path_in_container : {tool_base!r}   ({pic})\n"
                        f"      pipeline output        : {pipe_base!r}   ({pipe_path})"
                    )

    if mismatches:
        print("File-output basename MISMATCHES:")
        for m in mismatches:
            print("  " + m)
    if missing:
        print("Missing tool/label/mount wiring:")
        for m in missing:
            print("  " + m)

    if mismatches or missing:
        print(f"\nFAIL: {len(mismatches)} mismatch(es), {len(missing)} missing-wiring issue(s).")
        return 1

    print(f"OK: {checked} file output(s) checked across all pipelines; all filenames consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
