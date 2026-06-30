# NiChart Project API — CLAUDE.md

## Purpose

This repository contains a **Python FastAPI server** that replaces the backend of the
NiChart Streamlit application (`../NiChart_Project`, read-only reference). It exposes a
well-documented REST API consumed by a React UI (`../NiChart_UI`, read-only reference).

The application lets users run containerized medical-imaging processing pipelines, upload
imaging data, inspect results, and explore results interactively (centile plots, masked
overlays per feature, per-subject MRI views).

---

## Repository structure

```
NiChart_Project_API/
├── app/
│   ├── main.py               # app factory, health endpoint, router wiring
│   ├── config.py             # Settings (Pydantic BaseSettings, env-driven); NICHART_HOST_DATA_ROOT added
│   ├── auth/
│   │   ├── cognito.py        # JWKS cache + RS256 JWT verification
│   │   └── dependencies.py   # require_auth, public, CurrentUser
│   ├── routers/
│   │   ├── catalog.py        # list/get pipelines + tools
│   │   ├── cloud.py          # GET /cloud/status — local returns immediately; cloud queries Batch
│   │   ├── projects.py       # list, create, delete
│   │   ├── files.py          # listing, download, delete, NIfTI stage/commit, CSV/BIDS/IDAT upload, participants
│   │   ├── dicom.py          # upload, series inspect, convert (dcm2niix job), discard
│   │   └── jobs.py           # submit pipeline, list/get/logs/cancel runs
│   ├── services/
│   │   ├── path_security.py    # assert_safe_path, safe_unzip, PathEscapeError
│   │   ├── catalog_service.py  # YAML loading for tools + pipelines; load_tool_spec → ToolSpec
│   │   ├── file_service.py     # project CRUD, files, NIfTI staging, participants, BIDS reorganisation
│   │   ├── job_service.py      # in-memory run store, pipeline + direct-step orchestration tasks
│   │   └── dicom_service.py    # DICOM staging, pydicom header inspection, series file organisation
│   ├── backends/
│   │   ├── __init__.py        # get_backend FastAPI dependency
│   │   ├── base.py            # MountSpec, ToolSpec, JobHandle + JobBackend ABCs
│   │   ├── docker_backend.py  # DooD — sibling containers via host Docker socket
│   │   └── batch_backend.py   # cbica-nichart-submitjob Lambda + Batch status polling
│   └── models/                # all Pydantic request/response schemas
├── resources/
│   ├── pipelines/
│   │   └── dummy_pipeline.yaml   # single-step sleep pipeline for testing
│   └── tools/
│       ├── dummy_sleep.yaml      # alpine sleep tool for testing
│       └── dcm2niix.yaml         # dcm2niix DICOM→NIfTI conversion tool
├── tests/                     # 105 tests, all passing
│   ├── conftest.py            # RSA key fixtures, local_client, cloud_client, data_client,
│   │                          #   job_client (MockBackend), make_id_token
│   ├── test_health.py
│   ├── test_auth.py
│   ├── test_path_security.py
│   ├── test_routes_require_auth.py
│   ├── test_catalog.py
│   ├── test_projects.py
│   ├── test_files.py
│   ├── test_jobs.py
│   └── test_dicom.py
├── scripts/
│   ├── dev.sh                 # docker compose up
│   └── test.sh                # build dev image + pytest -v
├── docker-compose.yml
├── Dockerfile                 # multi-stage: base → dev / prod
├── pyproject.toml
├── .env.example
├── .gitignore
└── CLAUDE.md

```

### Run the test suite

```bash
docker compose run --rm -e NICHART_EXECUTION_MODE=local api pytest -v
```

---

## Implementation status

| Layer | Status |
|-------|--------|
| Infrastructure (Dockerfile, compose, pyproject) | Done |
| App factory + health endpoint | Done |
| Settings (`NICHART_*` env vars, incl. `NICHART_HOST_DATA_ROOT`) | Done |
| Auth — Cognito JWT verification | Done |
| Auth — `require_auth` / `public` dependencies | Done |
| Path security (`assert_safe_path`, `safe_unzip`) | Done |
| Pydantic models (all schemas) | Done |
| `ToolSpec` / `MountSpec` dataclasses + `JobHandle` / `JobBackend` ABCs | Done |
| `dummy_sleep` tool YAML + `dummy_pipeline` pipeline YAML | Done |
| `dcm2niix` tool YAML | Done |
| `README.md` + `scripts/dev.sh` + `scripts/test.sh` | Done |
| `catalog_service.py` — YAML loading for pipelines, tools, + `load_tool_spec()` | Done |
| `file_service.py` — project CRUD, files, NIfTI staging, participants, BIDS reorganisation | Done |
| `job_service.py` — in-memory run store, pipeline + direct-step orchestration | Done |
| `dicom_service.py` — staging upload, pydicom inspection, series organisation | Done |
| `docker_backend.py` — DooD sibling-container execution | Done (needs compose changes — see below) |
| `batch_backend.py` — Lambda invocation + Batch status polling | Done |
| `get_backend` FastAPI dependency | Done |
| Catalog router (`/catalog/...`) | Done |
| Projects router (`/projects`) | Done |
| Files router — listing, download, delete, NIfTI stage/commit/discard, CSV/IDAT/BIDS upload, participants | Done |
| DICOM router (`/projects/.../files/dicom/...`) | Done |
| Jobs router (`/jobs/pipelines/...`) | Done |
| `GET /cloud/status` — boto3 Batch query (running + pending counts + drain estimate) | Done |
| Test suite (105 tests, all passing) | Done |

### Pending infrastructure changes (needs user approval)

[No information here]

---

## Design constraints

### 1. Execution modes

Two deployment targets share the same API surface:

| Mode | Job backend | Storage |
|------|-------------|---------|
| **Cloud** | AWS Batch, submitted via Lambda `cbica-nichart-submitjob` | FSx for Lustre (transparent S3 mirror) |
| **Local** | Docker daemon on the user's machine | Local filesystem |

Mode is selected at startup via the `NICHART_EXECUTION_MODE` environment variable
(`cloud` or `local`). The `JobBackend` abstraction (see `backends/`) must be the only
place where this bifurcation lives.

#### Cloud Lambda interface (`cbica-nichart-submitjob`)

The batch backend invokes the Lambda with:

```json
{
  "id_token": "<Cognito ID token>",
  "tool_name": "nichart_dlmuse",
  "user_mounts": {
    "input":  "/fsx/fsx/{user_sub}/{project}/t1",
    "output": "/fsx/fsx/{user_sub}/{project}/dlmuse_vol"
  },
  "user_params": { "param_name": "value" },
  "num_subjects": 42
}
```

**The Lambda is the security boundary for command construction.** It loads the tool spec
from S3 (`cbica-nichart-staticfiles/tools/{tool_name}.yaml`), validates mount paths are
within `/fsx/fsx/{user_sub}/`, and generates the Docker command and image name itself.
The API server must never pass `image` or `command` fields — that would allow arbitrary
workloads.

The Lambda stores these Batch job parameters (readable via `describe_jobs`):

| Parameter | Content |
|-----------|---------|
| `tool_id` | `tool_name` from the request |
| `num_subjects` | `num_subjects` from the request (used for queue-drain estimation) |
| `FullCommand` | Generated Docker command (Lambda-internal, for auditability) |
| `ContainerImage` | Container image (Lambda-internal) |

The Batch queue is `cbica-nichart-jobqueue-standard`. The Lambda response body contains
`job_id` (the Batch job ID) which the backend uses as the `JobHandle` identifier.

### 2. Authentication (AWS Cognito)

- **Cloud mode**: The server uses a BFF OAuth2 flow with Cognito. Tokens are stored in
  httpOnly cookies (`session` = ID token, `refresh_token` = refresh token) set by
  `GET /auth/callback`. The browser sends the `session` cookie automatically with every
  request; the server verifies the JWT signature against the Cognito JWKS endpoint
  (cached). The Cognito `sub` claim is the canonical user identifier for storage isolation.
- **Local mode**: Auth verification is bypassed; a fixed synthetic user ID is used
  (`"LOCAL_USER"`).
- Auth is **on by default**. Endpoints safe to expose without auth (catalog, health) use
  an explicit `public` FastAPI dependency override — never silence auth by omission.
- The authenticated user context (`sub`, email, groups, raw token) must be available as
  a typed `CurrentUser` dependency from any route handler.

**Cognito configuration:**

| Parameter | Value |
|-----------|-------|
| Region | `us-east-1` |
| User Pool ID | `us-east-1_BSBhcKA66` |
| Identity Pool ID | `us-east-1:12c87a16-8336-450c-bf25-b98990c7dcf8` |
| JWKS URL | `https://cognito-idp.us-east-1.amazonaws.com/us-east-1_BSBhcKA66/.well-known/jwks.json` |
| OIDC config URL | `https://cognito-idp.us-east-1.amazonaws.com/us-east-1_BSBhcKA66/.well-known/openid-configuration` |

### 3. Path security — non-negotiable invariants

Every path that originates from user input or from a user-uploaded archive must be
validated before use:

- **Containment check**: the resolved (realpath) path must be a descendant of the user's
  data root (`NICHART_DATA_ROOT / user_id /`). No symlink traversal, no `..` components.
- **Zip/archive uploads**: after extracting to a temp directory, validate every member:
  - Reject if any member path is absolute.
  - Reject if any member path normalises to `..` or escapes the extraction root.
  - Reject if any member is a symlink (zip member attribute `external_attr`).
- Validation is centralised in `app/services/path_security.py` with a single
  `assert_safe_path(base: Path, target: Path)` that raises `PathEscapeError` (maps to
  HTTP 400) on violation, and a `safe_unzip(archive: Path, dest: Path)` that wraps
  extraction with all the above checks.
- These checks must run before **any** file I/O.

### 4. API documentation

FastAPI's built-in OpenAPI output must be production-quality:

- Every route has a `summary`, `description`, and meaningful `tags`.
- Every Pydantic model has docstrings and `Field(description=...)` on every field.
- Error responses are documented with `responses={}` on each route.
- The server must pass an OpenAPI schema lint (e.g. `openapi-spec-validator`) in CI.

### 5. Job / pipeline run model

**Pipeline runs are asynchronous.** The submit endpoint returns a `run_id` immediately.
The server spawns a background task that orchestrates the full pipeline (step-by-step,
matching the reference app's sequential execution with step caching). The client polls
for status.

#### Submission

```
POST /projects/{project_id}/jobs/pipelines
Body: { pipeline_id, params, reuse_cached_steps }
Returns: { run_id, status: "pending" }
```

#### Server-side background task

The background task:
1. Parses the pipeline YAML, resolves the execution DAG (currently linear with
   topological sort — see `parse_pipeline_steps` in reference app).
2. For each step in order: submits the tool job, polls until the job backend reports
   success/failure, records result in step metadata, then moves to the next step.
3. Updates the pipeline run record after each step.
4. On any step failure: marks the run as failed and stops.

The task is lightweight (mostly I/O-bound polling with `asyncio.sleep`), so
`asyncio.create_task` inside the request lifecycle is acceptable.

#### Pipeline run store

Runs are stored in memory as `dict[run_id, PipelineRunRecord]`. The record includes:
- `run_id` (UUID)
- `user_id`, `project_id`, `pipeline_id`
- `status`: `pending | running | succeeded | failed`
- `current_step` (step index), `total_steps`
- Per-step status and timestamps
- `submitted_at`, `finished_at`
- Error message if failed

The in-memory store is sufficient for MVP. A future enhancement could persist to a file
under `{study_dir}/_working/pipeline_runs.json` for durability across restarts.

`GET /projects/{project_id}/jobs/pipelines` returns the most recent N runs for the
authenticated user across all their projects (ordered by `submitted_at` descending).

`GET /projects/{project_id}/jobs/pipelines/{run_id}` returns full detail including
per-step status and logs.

#### Tool-level job status

Individual tool jobs (Docker container or Batch job) use the `TaskHandle` abstraction
from `backends/`. Log streaming uses `GET /projects/{project_id}/jobs/pipelines/{run_id}/logs`.

#### Step caching

Inherited from the reference app: step results are recorded in
`{study_dir}/_working/metadata.json`, keyed by `tool_id|inputs|params`. A step is
skipped when its record shows `status=success` and all input mtimes predate
`finished_time`. Controlled by `reuse_cached_steps` in the submit body.

### 6. Storage layout

```
NICHART_DATA_ROOT/
└── {user_sub}/
    └── {project_name}/
        ├── t1/                ← T1 NIfTI images ({mrid}.nii.gz)
        ├── fl/                ← FLAIR images
        ├── t2/
        ├── t1ce/
        ├── adc/
        ├── idat/
        ├── participants/
        │   └── participants.csv
        ├── _upload/           ← staging area (cleaned after processing)
        │   ├── nifti/
        │   ├── dicoms/        ← staged DICOM zips, awaiting inspection/conversion
        │   ├── bids/
        │   └── idat/
        ├── _working/          ← metadata.json, inferred_data_paths.csv, etc.
        └── <tool_outputs>/    ← e.g. dlmuse_vol/, dlmuse_seg/, ml_biomarkers/
```

`${STUDY}` in pipeline YAMLs resolves to `NICHART_DATA_ROOT/{user_sub}/{project_name}`.

### 7. Upload types

| Type | Upload format | Endpoint | Disposition |
|------|--------------|----------|-------------|
| NIfTI (single or batch) | `.nii` / `.nii.gz` files | `POST /projects/{id}/files/upload/nifti` | Staged in `_upload/nifti/`; caller then confirms MRID + modality mapping via a follow-up `POST /files/commit/nifti` |
| Participants CSV | `.csv` | `POST /projects/{id}/files/upload/csv` | Written directly to `participants/participants.csv` (overwrite with warning) |
| DICOM | `.zip` | `POST /projects/{id}/files/upload/dicom` | Staged in `_upload/dicoms/`; see DICOM workflow below |
| BIDS folder | `.zip` containing BIDS layout | `POST /projects/{id}/files/upload/bids` | Extracted with security checks → reorganised into NiChart layout |
| IDAT files | `.zip` or multiple `.idat` | `POST /projects/{id}/files/upload/idat` | Extracted to `idat/` |

### 8. DICOM workflow

DICOM → NIfTI conversion requires user interaction to map DICOM series to NiChart
modalities, and must not run dcm2niix inline on the API server (too heavy). Design:

1. **Upload**: `POST /projects/{id}/files/upload/dicom`
   - Receives a `.zip` of DICOMs, extracts to `_upload/dicoms/` with security checks.
   - Returns a `staging_id`.

2. **Inspect**: `GET /projects/{id}/files/dicom/{staging_id}/series`
   - Reads DICOM headers via `pydicom` (lightweight, no conversion).
   - Returns a series list: `[{ series_uid, series_description, modality, study_date, patient_id, num_files }]`.
   - This runs server-side but is fast (header reads only).

3. **Convert**: `POST /projects/{id}/files/dicom/{staging_id}/convert`
   - Body: `{ series_mappings: [{ series_uid, nichart_modality: "t1" | "fl" | "t2" | "t1ce" | "adc" }] }`
   - Submits a `dcm2niix` tool job (Docker/Batch) for each selected series with the
     appropriate output directory.
   - Returns `{ run_id }` for status polling via the jobs API.
   - On completion, NIfTIs land in `{study}/{modality}/`.

The `dcm2niix` tool needs a corresponding YAML in `resources/tools/`. The conversion job
is tracked identically to any other tool job.

### 9. File listing and download

- `GET /projects/{id}/files` — returns a tree of the project directory (excluding
  `_upload/` internals and `_working/`), with file types, sizes, and mtimes.
- `GET /projects/{id}/files/download?path={relative_path}` — downloads a single file.
  Path is validated against the user's project root.
- `GET /projects/{id}/files/download?path={relative_dir}&zip=true` — streams an
  on-the-fly zip of the specified directory subtree. No zip is written to disk; it is
  streamed directly to the client using `zipfile` + `StreamingResponse`.

### 10. Tool spec model (from reference app)

Tool YAML fields (`resources/tools/*.yaml`):

```yaml
name: str
description: str
inputs:   { label: { type: file|directory } }
outputs:  { label: { type: file|directory } }
mounts:   { label: { path_in_container: str, mode: ro|rw } }
resources: { vcpus: int, memory: int (MiB), gpus: int }
container: { image: str, command: str }   # command uses {mount_label} + {param} substitution
parameters: { name: { type: int|float|bool|str, default: ..., choices: [...] } }
time_per_subject_seconds: float | null    # optional; used by GET /cloud/status for queue-drain estimates
```

Pipeline YAML fields (`resources/pipelines/*.yaml`):

```yaml
pipeline_name: str
description: str
categories: [str]
requires: [str | { str: params }]   # e.g. needs_T1, csv_has_columns: [MRID, Age]
steps:
  - id: str
    tool: str                        # matches a tool YAML basename
    inputs:  { label: "${STUDY}/..." | "${step_id.outputs.label}" }
    outputs: { label: "${STUDY}/..." }
    params:  { name: value }
```

---

### 11. NIfTI upload — staging + commit flow

NIfTI upload always goes through a two-step staging flow. This unifies single-file and
batch cases: the server infers metadata from filenames and the client presents the
proposals for user confirmation before committing.

**Step 1 — Upload:**
```
POST /projects/{project_id}/files/upload/nifti
Content-Type: multipart/form-data
files: [...]   (one or many .nii / .nii.gz)
→ { staging_id, proposals: [{ filename, inferred_mrid, inferred_modality }] }
```
Files land in `{study}/_upload/nifti/`. Inference:
- `inferred_mrid`: strip known suffixes (`_T1`, `_T1w`, `_FL`, `_FLAIR`, `_T2`, `_T1CE`,
  `_ADC`, `.nii`, `.nii.gz`) and common trailing separators.
- `inferred_modality`: detect from filename substring (`_T1`→`t1`, `_FL`/`_FLAIR`→`fl`,
  `_T2`→`t2`, `_T1CE`→`t1ce`, `_ADC`→`adc`); `null` if not detectable.

**Step 2 — Commit:**
```
POST /projects/{project_id}/files/stage/{staging_id}/commit
{ mappings: [{ filename, mrid, modality }] }
→ { committed: [{ mrid, modality, path }] }
```
Each file is moved from staging to `{study}/{modality}/{mrid}.nii.gz`. MRID and modality
are mandatory in the confirmed mapping (no implicit inference at commit time).

**Discard:**
```
DELETE /projects/{project_id}/files/stage/{staging_id}
```
Removes the staged files. Staging areas are also auto-cleaned after a configurable TTL
(default: 24 h), controlled by `NICHART_STAGING_TTL_HOURS`.

### 12. Project identity

Project name = project directory name = URL path segment identifier. The server enforces
that project names match `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$` (URL-safe, filesystem-safe,
no leading special characters). No separate opaque UUIDs for projects.

### 13. Job API structure

```
POST   /projects/{project_id}/jobs/pipelines         → submit (scoped to project)
GET    /jobs/pipelines?project_id=...                → list runs (optional filter, most recent first)
GET    /jobs/pipelines/{run_id}                      → full detail + per-step status
GET    /jobs/pipelines/{run_id}/logs                 → aggregated logs
DELETE /jobs/pipelines/{run_id}                      → cancel
```

---

## Complete API surface

### Public (no auth required)
```
GET  /health
GET  /catalog/pipelines
GET  /catalog/pipelines/{pipeline_id}
GET  /catalog/tools/{tool_id}
```

### Projects
```
GET    /projects
POST   /projects                          body: { name }
DELETE /projects/{project_id}
```

### Files
```
GET    /projects/{project_id}/files
GET    /projects/{project_id}/files/download   ?path=rel_path [&zip=true]
DELETE /projects/{project_id}/files            ?path=rel_path

POST   /projects/{project_id}/files/upload/nifti        → staging
POST   /projects/{project_id}/files/stage/{id}/commit   → commit mapping
DELETE /projects/{project_id}/files/stage/{id}          → discard staging

POST   /projects/{project_id}/files/upload/csv
POST   /projects/{project_id}/files/upload/bids         → .zip
POST   /projects/{project_id}/files/upload/idat         → .zip or multipart .idat

GET    /projects/{project_id}/participants
PATCH  /projects/{project_id}/participants               body: { rows: [...] }
```

### DICOM
```
POST   /projects/{project_id}/files/upload/dicom              → { staging_id }
GET    /projects/{project_id}/files/dicom/{staging_id}/series → series list
POST   /projects/{project_id}/files/dicom/{staging_id}/convert → { run_id }
DELETE /projects/{project_id}/files/dicom/{staging_id}
```

### Jobs
```
POST   /projects/{project_id}/jobs/pipelines           body: { pipeline_id, params?, reuse_cached_steps? }
GET    /jobs/pipelines                                 ?project_id=...
GET    /jobs/pipelines/{run_id}
GET    /jobs/pipelines/{run_id}/logs
DELETE /jobs/pipelines/{run_id}
```

---

## Decisions deferred / requiring discussion

- DICOM staging cleanup policy: default TTL is 24 h (configurable); explicit DELETE also available.
- Participants CSV edit: PATCH does a full replace for now. Row-level merge is a future option.
- Results / viewer API (centile data, NIfTI slice serving) — **separate future phase**.
- Pipeline run store persistence across restarts — in-memory MVP; file-backed future option.
- Max upload size limits and multipart chunking for very large files.

---

## Working rules

- **Do only what is asked.** Ask before refactoring, cleaning up adjacent code, or adding
  features not in scope.
- **Ask before any change that affects shared infrastructure** (Docker setup, dependency
  versions, directory layout).
- **Isolated dev environment.** All development in Docker; no host-native installs.
- **No half-finished implementations.** Stubs are fine (raise `NotImplementedError`),
  but partial implementations that silently misbehave are not.
- **Read-only references**: `../NiChart_Project` and `../NiChart_UI` — never modify.
