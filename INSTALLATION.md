# Installation

How to install and configure the NiChart Project API and its `nichart` CLI, for
each execution backend (local Docker, local Singularity/Apptainer, SLURM, and
AWS Batch/cloud).

- New to the project / local Docker dev? `docs/getting-started.md` is the quick start.
- Cloud (AWS Batch) testing? `docs/cloud-local-testing.md`.
- Validating the CLI on a SLURM cluster? `docs/cli-slurm-testing.md`.

---

## 1. Execution backends

The server runs one job backend, chosen from config (see §4). Pick your target
before installing so you know which prerequisites you need.

| Backend | When | Runs tools via | Needs |
|---------|------|----------------|-------|
| `docker` | Local dev / single workstation | Docker daemon | Docker |
| `singularity` | Workstation/cluster without Docker | Apptainer/Singularity + prebuilt `.sif` | apptainer, SIF images |
| `slurm` | Shared HPC cluster | `sbatch` → Apptainer on compute nodes | SLURM CLIs, apptainer, SIF images, shared FS |
| `batch` | Cloud | AWS Batch via Lambda | AWS account/creds (see cloud-local doc) |

`docker` and `batch` have their own docs; **this file focuses on the Singularity
and SLURM paths**, which is where installation is non-obvious.

---

## 2. Prerequisites

Common:
- **Python 3.11+** (3.12 is the reference).
- Git checkout of this repository.

Per backend, additionally:
- **docker** → a working Docker daemon (and `NICHART_HOST_DATA_ROOT` if the server itself runs in a container — DooD).
- **singularity** → `apptainer` (or `singularity`) on `PATH`, and SIF images built (see §5).
- **slurm** → `sbatch`/`squeue`/`sacct` on `PATH` on the submit host; `apptainer` on the **compute** nodes; SIF images; and a **filesystem shared** between submit host and compute nodes for the data root and SLURM logs.
- **batch** → see `docs/cloud-local-testing.md`.

---

## 3. Install the package

**Install editable, from the repo root:**

```bash
python -m venv .venv && source .venv/bin/activate    # or conda
pip install -e .
```

> ⚠️ **Editable (`-e`) is required, not optional.** The package is monolithic —
> installing it gives you both the server (`app.main`, FastAPI/uvicorn) and the
> `nichart` CLI. The CLI's auto-spawn feature and the server's relative
> `resources/` path both resolve files **relative to the repo root** (the parent
> of the installed `app/`). A non-editable/wheel install puts `app/` in
> `site-packages`, where there is no `.env` and no `resources/` — the server would
> start with an empty pipeline/tool catalog. Install editable so the repo (with
> `.env` and `resources/`) stays the package root.

Verify:

```bash
nichart --help
python -c "import app.cli; print('repo root:', app.cli.REPO_ROOT)"
# repo root: /path/to/NiChart_Project_API   ← must contain .env and resources/
```

---

## 4. Configuration (`.env`)

The server reads all settings from environment variables prefixed `NICHART_`, and
loads a **`.env` file at the repo root** automatically. Set this up **once** at
install time — the CLI's spawned server reuses it, so runs are reproducible for
everyone using the install.

```bash
cp .env.example .env
$EDITOR .env
```

**Precedence (highest first):** exported `NICHART_*` env vars → repo `.env` →
built-in defaults. Check for stray overrides before relying on `.env`:

```bash
env | grep NICHART_ || echo "clean"
```

Key variables (see `.env.example` / `app/config.py` for the full list):

| Variable | Applies to | Notes |
|----------|-----------|-------|
| `NICHART_EXECUTION_MODE` | all | `local` or `cloud`. Default `local`. |
| `NICHART_JOB_BACKEND` | all | Explicit backend: `docker`\|`singularity`\|`slurm`\|`batch`. Overrides auto-selection. |
| `NICHART_DATA_ROOT` | all | Project data root. **Must be on shared FS for SLURM.** |
| `NICHART_SIF_DIR` | singularity, slurm | Directory of prebuilt `.sif` images (see §5). |
| `NICHART_CONTAINER_RUNNER` | singularity, slurm | `apptainer` (default) or `singularity`. |
| `NICHART_SLURM_PARTITION` / `_ACCOUNT` | slurm | Partition / account to charge. |
| `NICHART_SLURM_LOGS_DIR` | slurm | Job logs. Defaults to `<data_root>/_slurm_logs`. **Must be on shared FS.** |
| `NICHART_SLURM_EXTRA_SBATCH_ARGS` | slurm | JSON list, e.g. `["--constraint=h100"]`. |

**Backend auto-selection** when `NICHART_JOB_BACKEND` is unset:
`execution_mode=cloud` → `batch`; `local` + `NICHART_SIF_DIR` set → `singularity`;
`local` (default) → `docker`.

So a minimal **SLURM** `.env` looks like:

```ini
NICHART_JOB_BACKEND=slurm
NICHART_SIF_DIR=/shared/nichart/sif
NICHART_CONTAINER_RUNNER=apptainer
NICHART_DATA_ROOT=/shared/nichart/data
NICHART_SLURM_LOGS_DIR=/shared/nichart/slurm_logs
NICHART_SLURM_PARTITION=gpu
NICHART_SLURM_ACCOUNT=my_alloc
```

---

## 5. Building SIF images (Singularity / SLURM)

The Singularity and SLURM backends do **not** pull Docker at run time. Instead,
each tool's Docker image is converted once into a Singularity/Apptainer `.sif`
file, and the backend runs those. You must build this registry before the first
run (and refresh it when tool container versions change).

`scripts/build-sif-registry.py` reads every `resources/tools/*.yaml`, extracts its
`container.image` tag, and builds one `.sif` per unique image into `--sif-dir`:

```
alpine:3.19                          →  alpine_3.19.sif
cbica/nichart_dlmuse:1.0.10-wrapped  →  cbica_nichart_dlmuse_1.0.10-wrapped.sif
```

The naming is canonical — the server locates SIFs by this same convention, so
don't rename them.

### Usage

```bash
# Preview what would be built (no network, no build):
python scripts/build-sif-registry.py --dry-run

# Build into $NICHART_SIF_DIR (or ./sif):
python scripts/build-sif-registry.py --sif-dir /shared/nichart/sif --runner apptainer

# Refresh after a tool image bump:
python scripts/build-sif-registry.py --sif-dir /shared/nichart/sif --force
```

Options: `--tools-dir` (default `resources/tools` or `$NICHART_RESOURCES_PATH/tools`),
`--sif-dir` (default `$NICHART_SIF_DIR` or `./sif`), `--runner apptainer|singularity`,
`--force`, `--dry-run`.

Requirements: `apptainer`/`singularity` on `PATH`, `pyyaml`, **outbound network**
to pull the Docker images, and support for unprivileged builds (the script uses
`apptainer build --fakeroot`).

### ⚠️ On many clusters you must build as a SLURM job

Interactive/login nodes frequently **disallow `apptainer build` / `--fakeroot`**
(and often lack outbound network or enough scratch). If `python
scripts/build-sif-registry.py` fails on the login node with a fakeroot/permission
or network error, run it on a compute node via `sbatch`. Example
`build-sifs.sbatch`:

```bash
#!/bin/bash
#SBATCH --job-name=nichart-sif-build
#SBATCH --partition=<a-partition-that-allows-builds-and-has-network>
#SBATCH --time=03:00:00
#SBATCH --mem=16G
#SBATCH --cpus-per-task=4
#SBATCH --output=nichart-sif-build-%j.log

set -euo pipefail
module load apptainer          # cluster-specific; may be `singularity` or nothing
source /path/to/NiChart_Project_API/.venv/bin/activate
cd /path/to/NiChart_Project_API

export NICHART_SIF_DIR=/shared/nichart/sif
python scripts/build-sif-registry.py --sif-dir "$NICHART_SIF_DIR" --runner apptainer
```

```bash
sbatch build-sifs.sbatch
squeue -u "$USER"          # watch it
tail -f nichart-sif-build-*.log
```

The specifics (which partition allows builds, whether `--fakeroot` works or you
need a different unprivileged-build setup, module names, network/proxy) are
**cluster-dependent** — that's a call for whoever does the install. The point of
this doc is that the step exists and where it fits: build the SIF registry to a
**shared** `NICHART_SIF_DIR` (readable by compute nodes), then set that path in
`.env`.

---

## 6. Running the server

- **Local Docker (dev):** `docker compose up` (see `docs/getting-started.md`).
- **Singularity / SLURM (bare-metal, no compose):** run uvicorn from the repo root
  so it loads `.env` and `resources/`:
  ```bash
  cd /path/to/NiChart_Project_API
  uvicorn app.main:app --host 127.0.0.1 --port 8000
  ```
  For a shared, long-lived server, run it under `tmux`/`systemd`. For one-off CLI
  use you usually don't start it yourself — see §7.
- **Cloud (Batch):** see `docs/cloud-local-testing.md`.

Verify: `curl http://127.0.0.1:8000/health` → `{"status":"ok","execution_mode":"local",…}`.

---

## 7. The `nichart` CLI

Installed with the package (§3). Full reference: `app/CLI.md`; the all-in-one
`run` command: `app/CLI_run.md`.

- Point it at a server with `--url` or `NICHART_API_URL` (default
  `http://localhost:8000`).
- **`nichart run` can start a server for you.** With `--server auto` (default) it
  attaches to a running server if one answers, otherwise it spawns an ephemeral
  local one **from the repo root** (so it uses your `.env` and `resources/`), runs,
  waits for completion, and shuts it down. This is why the editable install (§3)
  and repo-root `.env` (§4) matter. `--server attach` requires an existing server;
  `--server spawn` always starts a fresh one.

```bash
# One shot: create a project, upload, verify, submit — spawning a local
# (SLURM-backed, per your .env) server automatically:
nichart run run_dlmuse --project study1 --t1 /shared/data/t1 --participants demo.csv --wait-until-done
```

> Multi-user note: in local mode the server has **no auth** and treats every
> request as the OS user running the server process. The per-user ephemeral spawn
> (default) therefore isolates users naturally (each runs as themselves). Do **not**
> stand up one shared local-mode server for a team expecting per-user isolation.

---

## 8. Post-install verification checklist

1. `nichart --help` and `python -c "import app.cli; print(app.cli.REPO_ROOT)"` → repo root with `.env` + `resources/`.
2. `env | grep NICHART_` → no stray overrides masking `.env`.
3. Backend prerequisites present (`sbatch`/`apptainer` for SLURM, etc.).
4. `NICHART_SIF_DIR` populated (`ls $NICHART_SIF_DIR/*.sif`) for singularity/slurm.
5. `NICHART_DATA_ROOT` and `NICHART_SLURM_LOGS_DIR` on shared storage (SLURM).
6. End-to-end smoke test: `docs/cli-slurm-testing.md`.
