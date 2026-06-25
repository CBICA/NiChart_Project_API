# Containerize a tool for NiChart

You are helping the user integrate an external application into the NiChart Project API
as a runnable pipeline tool. The tool to wrap is: **$ARGUMENTS**

Your job is to produce:
1. A **wrapper script** (if the upstream tool's interface does not match NiChart's
   directory-based I/O convention — this is the common case)
2. A **Dockerfile** that installs the upstream tool and bakes in the wrapper
3. A **tool YAML** in the NiChart API repo's `resources/tools/` that registers the tool

**File placement:**
- The wrapper script and Dockerfile are created **in the current working directory**
  (i.e. the tool's own repository or wherever the user invoked this skill from).
- The tool YAML is written to the NiChart API repository at
  `/home/agetka/vs-code/projects/NiChart_Project_API/resources/tools/<toolname>.yaml`.
- If the NiChart API repo is not at that path, ask the user where it is before writing.

Work through the steps below in order. At each step, ask the user for the information
you do not yet have before writing code.

---

## Step 1 — Understand the upstream tool's interface

Ask (or infer from context) the following:

- **How is it installed?** (pip, conda, apt, binary download, existing Docker image, etc.)
- **How does it receive input?**
  - A single file path? (`--input subject.nii.gz`)
  - A directory of files? (`--input_dir /data/t1/`)
  - A glob/list? (`--files a.nii b.nii`)
  - stdin?
- **What modalities does it need?** (T1 only? T1 + FLAIR? Something else?)
- **How does it produce output?**
  - Writes to an output directory the caller specifies? (`--out_dir /output/`)
  - Writes next to input files (side-car outputs)?
  - Writes to a fixed hardcoded path?
- **Are there required parameters** the user will need to control at runtime?
- **Is there already a public Docker image?** If so, what is its entrypoint/CMD?

---

## Step 2 — Determine whether a wrapper is needed

**NiChart's I/O contract for every tool:**

- Each input mount is a **directory** on the host that is bind-mounted read-only
  inside the container at the path declared in the tool YAML's `mounts` section.
- Each output mount is a **directory** bind-mounted read-write.
- Filenames inside the input directory follow the pattern `{MRID}.nii.gz`
  (one file per subject, all subjects batched in the same directory).
- The tool runs once per batch (not once per subject), so it must handle the
  whole directory itself.

A wrapper is **required** when any of the following is true:

| Upstream behavior | Wrapper needed because |
|---|---|
| Expects a single file path per invocation | Must loop over `{input_dir}/*.nii.gz` |
| Expects a non-standard filename convention | Must rename or symlink before calling |
| Writes output next to input (side-cars) | Must collect and move outputs to the output dir |
| Writes to a hardcoded path | Must redirect or copy to the NiChart output dir |
| Needs multiple separate input directories per subject | Must resolve paired files across mounts |
| Has a non-Docker entrypoint (conda env, singularity, etc.) | Must activate environment first |

If the tool already accepts a directory of `{MRID}.nii.gz` files as input and writes
to a caller-specified output directory, **no wrapper is needed** — the YAML `command`
field can call the tool directly using `{mount_label}` substitution.

---

## Step 3 — Write the wrapper script

If a wrapper is needed, write a shell script (preferred) or Python script. Name it
`run_wrapper.sh` (or `run_wrapper.py`). It will live inside the Docker image at
`/opt/nichart/run_wrapper.sh`.

The wrapper must:

1. Read from the NiChart input mount (e.g. `/input/t1/`) — iterate over `*.nii.gz`.
2. For each subject file, derive the MRID: `basename subject.nii.gz .nii.gz`.
3. Call the upstream tool in whatever format it expects.
4. Place outputs in the NiChart output mount (e.g. `/output/`) with the naming
   convention the pipeline YAML expects (often `{MRID}_<suffix>.nii.gz` or a CSV).
5. Exit non-zero if any subject fails (so the API marks the step as failed).

**Shell wrapper skeleton:**

```bash
#!/bin/bash
set -euo pipefail

INPUT_DIR="${1:?INPUT_DIR required}"    # filled by NiChart via {input} mount path
OUTPUT_DIR="${2:?OUTPUT_DIR required}"  # filled by NiChart via {output} mount path

mkdir -p "$OUTPUT_DIR"

for nii in "$INPUT_DIR"/*.nii.gz; do
    [ -e "$nii" ] || { echo "No .nii.gz files found in $INPUT_DIR"; exit 1; }
    mrid=$(basename "$nii" .nii.gz)

    echo "Processing $mrid..."
    # Replace this with the actual upstream tool call:
    upstream_tool \
        --input "$nii" \
        --output_dir "$OUTPUT_DIR" \
        --subject_id "$mrid"
done

echo "Done."
```

Adjust the upstream call as needed. If the tool produces output next to the input,
add a `mv` step after the call to relocate files into `$OUTPUT_DIR`.

---

## Step 4 — Write the Dockerfile

Prefer starting from an existing upstream image if one exists. If not, start from a
suitable base and install the tool.

```dockerfile
# ── Stage 1: upstream tool (replace with real base if available) ───────────────
FROM python:3.11-slim AS base

# Install the upstream tool however is appropriate
RUN pip install --no-cache-dir <upstream-package>
# or: RUN apt-get update && apt-get install -y <package>

# ── Stage 2: add NiChart wrapper ──────────────────────────────────────────────
FROM base

COPY run_wrapper.sh /opt/nichart/run_wrapper.sh
RUN chmod +x /opt/nichart/run_wrapper.sh

ENTRYPOINT ["/opt/nichart/run_wrapper.sh"]
```

If the upstream already has a Docker image:

```dockerfile
FROM upstream/image:tag

COPY run_wrapper.sh /opt/nichart/run_wrapper.sh
RUN chmod +x /opt/nichart/run_wrapper.sh

ENTRYPOINT ["/opt/nichart/run_wrapper.sh"]
```

Build and tag:

```bash
docker build -t nichart_<toolname>:<version>-wrapped .
```

The `-wrapped` suffix is the project convention for images that include a NiChart wrapper.

---

## Step 5 — Write the tool YAML

Write this file to:
`/home/agetka/vs-code/projects/NiChart_Project_API/resources/tools/<toolname>.yaml`

The canonical format:

```yaml
name: <Human-readable name>
description: >
  One or two sentences describing what this tool does and what outputs it produces.

inputs:
  <mount_label>:          # e.g. "t1_img"
    type: directory

outputs:
  <output_label>:         # e.g. "results"
    type: directory

mounts:
  <mount_label>:
    path_in_container: /input/t1   # where NiChart mounts the input dir inside the container
    mode: ro
  <output_label>:
    path_in_container: /output     # where NiChart mounts the output dir inside the container
    mode: rw

parameters:
  {}   # or list named parameters the user can override at submit time

resources:
  vcpus: 4          # how many vCPUs the tool needs
  memory: 8192      # MiB; be generous for imaging tools
  gpus: 0           # set to 1 if the tool requires a GPU

time_per_subject_seconds: null   # set to a float (e.g. 180.0) if known; used for queue-drain estimates

container:
  image: "nichart_<toolname>:<version>-wrapped"
  command: "{<mount_label>} {<output_label>}"
  # The command is passed to the container's ENTRYPOINT.
  # {mount_label} is substituted with path_in_container at runtime.
  # For tools with no wrapper (direct CLI call), write the full command here
  # and omit the ENTRYPOINT override in the Dockerfile.
```

**Command substitution rules** (from `ToolSpec.render_command`):
- `{mount_label}` → replaced with `path_in_container` for that mount
- `{param_name}` → replaced with the resolved parameter value
- The rendered string is passed to the container as-is (shell or entrypoint args)

**Multiple input modalities** (e.g. T1 + FLAIR):

```yaml
inputs:
  t1_img:
    type: directory
  fl_img:
    type: directory
mounts:
  t1_img:
    path_in_container: /input/t1
    mode: ro
  fl_img:
    path_in_container: /input/fl
    mode: ro
```

The wrapper receives both paths and is responsible for pairing files by MRID.

---

## Step 6 — Test locally

```bash
# Build the image
docker build -t nichart_<toolname>:test-wrapped .

# Create test directories with a dummy subject
mkdir -p /tmp/test_input /tmp/test_output
cp /path/to/subject001.nii.gz /tmp/test_input/

# Run manually (mirrors what docker_backend.py does)
docker run --rm \
  -v /tmp/test_input:/input/t1:ro \
  -v /tmp/test_output:/output:rw \
  nichart_<toolname>:test-wrapped \
  /input/t1 /output

# Check output
ls /tmp/test_output
```

Then register the tool in a pipeline YAML under
`/home/agetka/vs-code/projects/NiChart_Project_API/resources/pipelines/`
and submit a test job through the API using dummy data:

```bash
curl -X POST http://localhost:8000/projects/<project_id>/jobs/pipelines \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"pipeline_id": "<pipeline_yaml_stem>", "params": {}, "reuse_cached_steps": false}'
```

---

## Common pitfalls

- **Side-car outputs**: some tools write `subject001_seg.nii.gz` next to the input.
  The wrapper must move these into the declared output directory.
- **Fixed output filenames**: if the tool always writes `output.csv`, the wrapper must
  rename it to something MRID-keyed or the pipeline step-cache will not distinguish subjects.
- **Conda environments**: if the tool requires conda, install it in the Dockerfile and
  call it as `conda run -n <env> upstream_tool ...` inside the wrapper.
- **GPU availability**: set `gpus: 1` in resources and test with `docker run --gpus all`.
- **Cloud mode**: the Lambda validates that mount paths are inside `/fsx/fsx/{user_sub}/`.
  No code changes are needed — the API server constructs those paths automatically.
  Do not embed absolute host paths anywhere in the wrapper or YAML.
- **Parameter validation**: free-entry string parameters injected into the command template
  must match `^[a-zA-Z0-9._-]{1,256}`. Enforce this in the tool YAML by setting `choices`
  if the values are enumerable, so the API rejects invalid input before the container runs.
