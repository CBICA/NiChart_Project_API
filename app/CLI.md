# NiChart CLI

`nichart` is a terminal client for the NiChart API — create projects, upload
imaging and participant data, browse pipelines, submit jobs, and inspect results
without leaving the shell. It is implemented in [`cli.py`](cli.py) (Typer + Rich).

For the end-to-end **`nichart run`** command, see **[CLI_run.md](CLI_run.md)**.

---

## Prerequisites: a configured, running API server

`nichart` is a **client** — it does no processing itself. Almost every command
talks to a NiChart **API server**, which must be **installed and configured
first**, and (for most commands) **already running**.

1. **Set up the server → [INSTALLATION.md](../INSTALLATION.md).** Covers the
   editable install, the `.env`, the execution backend (Docker / Singularity /
   SLURM / Batch), and — for Singularity/SLURM — building the SIF images. Do this
   once, at install time.
2. **Start the server** and point the CLI at it:
   - Local dev: `docker compose up` (see [getting-started.md](../docs/getting-started.md)).
   - Bare-metal (no Docker): from the repo root, `uvicorn app.main:app --host 127.0.0.1 --port 8000` (INSTALLATION.md §6).
   - Verify: `nichart status` should report `ok`. Use `NICHART_API_URL` / `--url` if it isn't on `http://localhost:8000`.

> **`nichart run` can start the server for you — but it still needs configuring first.**
> `nichart run` will **automatically spin up a local API server if none is running,
> then shut it down when the run finishes** (`--server auto`, the default). That's a
> real convenience — you don't have to start the server by hand. But the server it
> spawns uses the **same configured install**: the editable install and the repo
> `.env` from [INSTALLATION.md](../INSTALLATION.md) must already be in place.
> Auto-spawn replaces *starting* the server, not *configuring* it. Details in
> [CLI_run.md](CLI_run.md).

---

## Install & invoke

The CLI ships with the API package. Once installed (`pip install -e .`), the
`nichart` entry point is available; during development you can also run it as a
module:

```bash
nichart --help
python -m app.cli --help      # equivalent, no install needed
```

## Configuration

| What | How | Default |
|------|-----|---------|
| API base URL | `NICHART_API_URL` env var, or `--url` on any command | `http://localhost:8000` |
| Data-path hints | `NICHART_DATA_ROOT` env var (optional) | unset |

`--url` overrides the env var per invocation:

```bash
nichart --url https://api.neuroimagingchart.com status
```

`NICHART_DATA_ROOT`, when set, lets the CLI print the absolute host path of a
project's data directory (`nichart data <project>`, and a hint in `files list` /
`projects create`). It is purely cosmetic — the CLI never reads or writes those
paths directly; all data access goes through the API.

### Authentication

- **Local mode** (`NICHART_EXECUTION_MODE=local` on the server): no auth; every
  request is the local user. The CLI works out of the box.
- **Cloud mode**: the API uses cookie-based (BFF) auth. The current CLI sends
  plain requests and does **not** manage the Cognito session cookie, so against a
  cloud deployment it only reaches public endpoints. Driving authenticated cloud
  endpoints from the CLI is not yet wired up.

## General behavior

- **Exit codes:** `0` on success (including a declined confirmation or a
  `--dry-run`), `1` on any error. Errors print `Error <status>: <detail>` from the
  API, or a specific client-side message.
- **Connection errors** print a clear "cannot connect — is the server running?"
  message rather than a stack trace.
- Output is rendered with Rich (tables, colour). Status colours: pending =
  yellow, running = cyan, succeeded = green, failed = red, skipped/cancelled = dim.

---

## Command reference

### Server / cloud

| Command | Description |
|---------|-------------|
| `nichart status` | Server health, mode, and version. |
| `nichart cloud` | Cloud queue status: running + pending jobs, drain estimate. Local mode reports `mode=local` with no queue data. |
| `nichart data <project>` | Print the absolute host path of a project's data dir (needs `NICHART_DATA_ROOT`). |

### Projects

| Command | Description |
|---------|-------------|
| `nichart projects list` | List your projects. |
| `nichart projects create <name>` | Create a project (name: alphanumeric, `-`, `_`). |
| `nichart projects delete <name> [-y]` | Delete a project and all its data (confirms unless `-y`). |

### Pipelines & tools (catalog)

| Command | Description |
|---------|-------------|
| `nichart pipelines list` | List available pipelines. |
| `nichart pipelines show <id>` | Pipeline detail: steps, parameters (type/default/range), requirements. |
| `nichart tools list` | List available tools. |
| `nichart tools show <id>` | Tool detail: inputs, outputs, resources, parameters. |

### Files

Each flag/argument is documented via `--help` on the subcommand.

| Command | Description |
|---------|-------------|
| `nichart files list <project>` | Tree of project files (type, size). |
| `nichart files download <project> <path> [-o OUT] [--zip]` | Download a file, or a directory subtree with `--zip`. |
| `nichart files delete <project> <path> [-y]` | Delete a file/directory. |
| `nichart files upload-nifti <project> <files...>` | Upload NIfTIs with **interactive** MRID/modality confirmation (infer → review → edit/commit/discard). |
| `nichart files upload-csv <project> <file>` | Upload the participants CSV (overwrites). |
| `nichart files upload-bids <project> <zip>` | Upload a BIDS zip (auto-reorganised). |
| `nichart files upload-idat <project> <zip>` | Upload an IDAT zip. |

> For **non-interactive** bulk NIfTI upload by fixed modality, prefer
> [`nichart run`](CLI_run.md), which uploads a whole directory per modality.

### Participants

| Command | Description |
|---------|-------------|
| `nichart participants show <project>` | Render the participants table (first 200 rows). |
| `nichart participants template <project> [-o OUT]` | Download a participants CSV template pre-filled with detected MRIDs. |

### Readiness & provenance

| Command | Description |
|---------|-------------|
| `nichart readiness <project> <pipeline>` | Whether the project has the imaging / CSV columns / subject count a pipeline needs, with per-check detail. |
| `nichart provenance <project> [-d]` | Whether cached step outputs are stale; `-d/--dirty-only` shows only problems. |

### Jobs

| Command | Description |
|---------|-------------|
| `nichart jobs` | Live dashboard of all your runs; polls until all shown runs are terminal (Ctrl+C to exit). |
| `nichart jobs <run_id>` | Live per-step detail for one run. |
| `nichart jobs submit <project> <pipeline> [-p k=v ...] [--no-wait] [--skip-readiness] [--no-reuse-cache]` | Submit and watch a run. |
| `nichart jobs logs <run_id>` | Aggregated logs (live for a running step in cloud mode). |
| `nichart jobs cancel <run_id>` | Request cancellation. |

`-p/--param` is repeatable and takes `key=value`; values are typed
`int → float → bool → str` automatically.

### Results

| Command | Description |
|---------|-------------|
| `nichart results list <project>` | Pipelines with results in the project (batch features? per-subject count, atlas?). |
| `nichart results show <project> <pipeline>` | Batch-feature availability + shape, per-subject output coverage, subject completeness. |

### Retention (cloud lifecycle)

| Command | Description |
|---------|-------------|
| `nichart retention show <project>` | When the project expires, and time remaining. |
| `nichart retention refresh <project>` | Reset the retention timer to the full window. |

### All-in-one

| Command | Description |
|---------|-------------|
| `nichart run <pipeline> --project <name> [modality flags] [--participants CSV] ...` | Create/select a project, upload data, verify readiness, and submit — one command. Can **auto-start a local server** if none is running (`--server auto`, the default), then shut it down when the run finishes. **See [CLI_run.md](CLI_run.md).** |

### Aliases

| Alias | Expands to |
|-------|-----------|
| `nichart submit <project> <pipeline> ...` | `nichart jobs submit ...` |
| `nichart watch <run_id>` | `nichart jobs <run_id>` |

---

## Not yet in the CLI

- **DICOM workflow** (`/files/dicom/...`: upload → inspect series → map
  series→modality → convert). This is an interactive multi-step flow; it has no
  CLI commands yet. Use the API/UI for DICOM, or upload converted NIfTIs.
- **Cloud authenticated sessions** — see the auth note above.
