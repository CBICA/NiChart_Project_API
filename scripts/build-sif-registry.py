#!/usr/bin/env python3
"""
Build Singularity/Apptainer SIF images from the Docker tags declared in tool YAMLs.

Run this script once on the target machine (cluster/workstation) before starting the
API server in Singularity mode.  Re-run with --force to refresh images when tool
container versions change.

Usage
-----
    python scripts/build-sif-registry.py [options]

Options
-------
    --tools-dir PATH    Tool YAML directory  (default: resources/tools, or $NICHART_RESOURCES_PATH/tools)
    --sif-dir PATH      Destination for .sif files  (default: $NICHART_SIF_DIR or ./sif)
    --runner CMD        'apptainer' or 'singularity'  (default: apptainer)
    --force             Rebuild existing SIFs
    --dry-run           Print what would be built without running anything

Naming convention
-----------------
Docker tag  →  SIF filename
  alpine:3.19                          →  alpine_3.19.sif
  cbica/nichart_dlmuse:1.0.10-wrapped  →  cbica_nichart_dlmuse_1.0.10-wrapped.sif
  registry.io/org/image:v1.0           →  registry.io_org_image_v1.0.sif

The API server uses the same function to locate SIFs at runtime, so the naming
convention must not change between builds.

Requirements
------------
  pip install pyyaml         (for YAML parsing)
  apptainer or singularity   (must be on PATH)
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is not installed. Run: pip install pyyaml")


def docker_tag_to_sif_name(image: str) -> str:
    """Convert a Docker image tag to the canonical SIF filename.

    Replaces '/' and ':' with '_'.  Dots are kept as-is.

    >>> docker_tag_to_sif_name("alpine:3.19")
    'alpine_3.19.sif'
    >>> docker_tag_to_sif_name("cbica/nichart_dlmuse:1.0.10-wrapped")
    'cbica_nichart_dlmuse_1.0.10-wrapped.sif'
    """
    return image.replace("/", "_").replace(":", "_") + ".sif"


def collect_images(tools_dir: Path) -> dict[str, str]:
    """Return {tool_id: docker_image} for every tool YAML that has a container.image."""
    result: dict[str, str] = {}
    for yaml_path in sorted(tools_dir.glob("*.yaml")):
        try:
            with yaml_path.open() as f:
                data = yaml.safe_load(f)
            image = (data.get("container") or {}).get("image", "").strip()
            if image:
                result[yaml_path.stem] = image
        except Exception as exc:
            print(f"  WARNING: could not parse {yaml_path.name}: {exc}", file=sys.stderr)
    return result


def build_sif(
    image: str,
    sif_path: Path,
    runner: str,
    force: bool,
    dry_run: bool,
) -> bool:
    """Pull a Docker image and convert it to SIF.  Returns True on success/skip."""
    if sif_path.exists() and not force:
        print(f"  SKIP     {sif_path.name}  (already exists; use --force to rebuild)")
        return True

    action = "DRY-RUN" if dry_run else "BUILD"
    print(f"  {action:<8} docker://{image}")
    print(f"           → {sif_path}")

    if dry_run:
        return True

    result = subprocess.run(
        [runner, "build", "--fakeroot", str(sif_path), f"docker://{image}"],
        check=False,
    )
    if result.returncode != 0:
        print(f"  ERROR    build failed (exit {result.returncode})")
        return False

    print(f"  OK       {sif_path.name}")
    return True


def main() -> None:
    default_sif_dir = os.environ.get("NICHART_SIF_DIR", "sif")
    default_tools_dir = Path(
        os.environ.get("NICHART_RESOURCES_PATH", "resources")
    ) / "tools"

    parser = argparse.ArgumentParser(
        description="Build Singularity SIF images from NiChart tool YAML definitions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tools-dir",
        type=Path,
        default=default_tools_dir,
        metavar="PATH",
        help=f"Tool YAML directory (default: {default_tools_dir})",
    )
    parser.add_argument(
        "--sif-dir",
        type=Path,
        default=Path(default_sif_dir),
        metavar="PATH",
        help=f"Directory to store SIF files (default: {default_sif_dir})",
    )
    parser.add_argument(
        "--runner",
        default="apptainer",
        choices=["apptainer", "singularity"],
        help="Container runner to use (default: apptainer)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild SIFs even if they already exist",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be built without executing anything",
    )
    args = parser.parse_args()

    if not args.tools_dir.is_dir():
        sys.exit(f"ERROR: tools directory not found: {args.tools_dir}")

    if not args.dry_run:
        args.sif_dir.mkdir(parents=True, exist_ok=True)

    images = collect_images(args.tools_dir)
    if not images:
        print(f"No tool images found in {args.tools_dir}")
        return

    # De-duplicate: multiple tools can share the same base image.
    unique: dict[str, str] = {}  # image → sif_name
    for tool_id, image in images.items():
        unique[image] = docker_tag_to_sif_name(image)

    print(f"SIF directory : {args.sif_dir}")
    print(f"Runner        : {args.runner}")
    print(f"Tools found   : {len(images)}  ({len(unique)} unique image(s))")
    print()

    errors = 0
    for image, sif_name in unique.items():
        ok = build_sif(
            image=image,
            sif_path=args.sif_dir / sif_name,
            runner=args.runner,
            force=args.force,
            dry_run=args.dry_run,
        )
        if not ok:
            errors += 1
        print()

    if errors:
        print(f"{errors} error(s).  Fix above issues and re-run (or use --force).")
        sys.exit(1)
    elif not args.dry_run:
        print("All SIF images built successfully.")
        print(f"Set NICHART_SIF_DIR={args.sif_dir.resolve()} when starting the API server.")


if __name__ == "__main__":
    main()
