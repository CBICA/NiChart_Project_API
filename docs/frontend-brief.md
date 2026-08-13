# NiChart UI — Frontend Development Brief

## What you're building

A React web application that lets users run containerised medical-imaging pipelines on their own data. The backend is a FastAPI server (`../NiChart_Project_API`). The full API surface is in `openapi.json` at the root of this repo.

A reference Streamlit implementation exists at `../NiChart_Project` — read it for UX reference (page flow, labels, what data is shown where), but do not copy its architecture. The goal is a proper React SPA, not a port.

---

## Connecting to the API

**Base URL:** `http://localhost:8000` (local mode, configurable)

**Auth:** In local mode (`NICHART_EXECUTION_MODE=local`) the server bypasses auth entirely — no credentials are needed. In cloud mode the Cognito `id_token` lives in the `session` httpOnly cookie, set by the `/auth/login` → `/auth/callback` BFF flow; the browser sends it automatically, so send requests with credentials included (e.g. `fetch(..., { credentials: "include" })`). There is no `Authorization: Bearer` header. For now, local mode is the target.

**Error shape:** All errors return `{ "detail": "..." }` (FastAPI default).

---

## Core concepts

### Projects

A project is a named workspace (`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`). All data lives under it. Create one before doing anything else.

### File uploads — four types

| Type | Endpoint | Notes |
|------|----------|-------|
| NIfTI | `POST /projects/{id}/files/upload/nifti` | Two-step: upload → review proposals → commit |
| CSV (participants) | `POST /projects/{id}/files/upload/csv` | Overwrites existing participants.csv |
| DICOM | `POST /projects/{id}/files/upload/dicom` | Three-step: upload → inspect series → convert |
| BIDS zip | `POST /projects/{id}/files/upload/bids` | Extracted and reorganised automatically |
| IDAT zip | `POST /projects/{id}/files/upload/idat` | Extracted to `idat/` directly |

### NIfTI staging flow (two steps, requires UI)

1. `POST /projects/{id}/files/upload/nifti` — returns `{ staging_id, proposals: [{ filename, inferred_mrid, inferred_modality }] }`. Files sit in a staging area.
2. Show the user a table of proposals. They can correct the MRID and modality (t1 / fl / t2 / t1ce / adc) for each file.
3. `POST /projects/{id}/files/stage/{staging_id}/commit` with `{ mappings: [{ filename, mrid, modality }] }` — moves files to their final locations.
4. `DELETE /projects/{id}/files/stage/{staging_id}` to discard.

### DICOM flow (three steps, requires UI)

1. `POST /projects/{id}/files/upload/dicom` — upload a `.zip`, get back `{ staging_id }`.
2. `GET /projects/{id}/files/dicom/{staging_id}/series` — returns a list of DICOM series with description, modality, date, patient ID, file count.
3. Show the user the series list so they can pick which series maps to which NiChart modality.
4. `POST /projects/{id}/files/dicom/{staging_id}/convert` with `{ series_mappings: [{ series_uid, nichart_modality }] }` — submits a dcm2niix conversion job, returns `{ run_id }`.
5. Poll the job via `GET /jobs/pipelines/{run_id}` until complete.

### Pipeline runs (async, polling)

1. `GET /catalog/pipelines` — list available pipelines (public, no auth).
2. `GET /catalog/pipelines/{id}` — full pipeline detail including `parameters` (with types, defaults, min/max, choices).
3. Before submitting, call `GET /projects/{project_id}/readiness/{pipeline_id}` to check whether the project has the required data. Shows missing modalities, missing CSV columns, etc.
4. `POST /projects/{project_id}/jobs/pipelines` with `{ pipeline_id, params, reuse_cached_steps }` — returns `{ run_id, status: "pending" }` immediately.
5. Poll `GET /jobs/pipelines/{run_id}` for `status` (`pending | running | succeeded | failed`) and per-step progress.
6. `GET /jobs/pipelines/{run_id}/logs` for log output.
7. `DELETE /jobs/pipelines/{run_id}` to cancel.

### Pipeline parameters

`GET /catalog/pipelines/{id}` returns a `parameters` dict. Each entry has:
- `type`: `"int"`, `"float"`, `"bool"`, `"str"`
- `default`: default value
- `min` / `max`: numeric bounds (render as a bounded input or slider)
- `choices`: if present, render as a dropdown (exact values only)
- `description`: tooltip text

Render a parameter form from this schema before showing the submit button.

### Participants CSV

- `GET /projects/{id}/participants` — returns `{ rows: [{ MRID, ...columns }] }`. MRID is always present; additional columns vary.
- `PATCH /projects/{id}/participants` with `{ rows: [...] }` — full replace. Use this after the user edits the table in-browser.

### File browser

- `GET /projects/{id}/files` — returns a directory tree (excludes staging internals).
- `GET /projects/{id}/files/download?path=rel/path` — download a single file.
- `GET /projects/{id}/files/download?path=rel/dir&zip=true` — stream a zip of a directory.
- `DELETE /projects/{id}/files?path=rel/path` — delete a file or directory.

### Cloud status (optional)

`GET /cloud/status` — in cloud mode returns queue depth and a drain-time estimate. In local mode returns immediately with empty counts. Safe to call; just skip rendering if `execution_mode == "local"`.

---

## Public endpoints (no auth token needed in any mode)

```
GET /health
GET /catalog/pipelines
GET /catalog/pipelines/{pipeline_id}
GET /catalog/tools/{tool_id}
```

---

## Suggested page structure

```
/                          → project list (or redirect to /projects)
/projects                  → list + create
/projects/:id              → project home: file browser, participants table, run history
/projects/:id/upload       → upload wizard (choose type → type-specific flow)
/projects/:id/pipelines    → browse catalog, check readiness, configure params, submit
/projects/:id/runs/:run_id → run detail: step progress, logs
```
