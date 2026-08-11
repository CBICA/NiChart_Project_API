# `nichart run` — one-command pipeline runs

`nichart run` is the simplest way to go from local files to a running pipeline.
It creates (or selects) a project, uploads your imaging and participant data,
runs every safety check, and submits the pipeline — with clear, actionable errors
at each step. It is defined in [`cli.py`](cli.py).

See [CLI.md](CLI.md) for the rest of the CLI.

```bash
nichart run <PIPELINE> --project <NAME> [modality flags] [--participants CSV] [options]
```

Example:

```bash
nichart run run_dlmuse --project study1 --t1 /data/t1
nichart run run_spare_all --project study1 \
    --t1 /data/t1 --fl /data/flair \
    --participants participants.csv \
    --wait-until-done
```

---

## What it does

Each step is verified before the next; a failure stops the command with a
specific message.

1. **Verify the pipeline exists.** Unknown pipeline → error + a pointer to
   `nichart pipelines list`.
2. **Create or select the project.** By default a *new* project is created;
   a name collision is an error (use `--existing` to add to it instead). With
   `--existing`, a *missing* project is an error.
3. **Upload each provided modality** (`--t1 / --fl / --t2 / --t1ce / --adc / --pet`,
   or `--image MOD=PATH` for any other) as
   NIfTI. Each flag takes a **flat directory** (every `.nii`/`.nii.gz` inside) or
   a **single file**. MRIDs are inferred from filenames server-side; a file whose
   MRID can't be inferred aborts the run (the staged batch is discarded and the
   offending files are listed).
4. **Upload the participants CSV** (`--participants`), if given.
5. **Cross-modality diagnostics.** If subject counts differ between modalities,
   it prints the per-modality counts and exactly which MRIDs are missing from
   which modality.
6. **Readiness check.** Renders imaging / CSV-column / subject-count results.
   If unsatisfied, the run stops unless `--force` is given.
7. **Submit the pipeline.**
8. **Hand back tracking**, or — with `--wait-until-done` — stream live per-step
   progress until the run finishes.

If a *newly created* project fails partway through setup, the command tells you
the project was left with partial data and prints the exact
`nichart projects delete …` to remove it. (It does not auto-delete, so you can
inspect the partial state.)

---

## Arguments & options

| Flag | Type | Default | Meaning |
|------|------|---------|---------|
| `PIPELINE` (positional) | str | — | Pipeline ID to run (`nichart pipelines list`). |
| `--project`, `-P` | str | — (**required**) | Project name. Created new unless `--existing`. |
| `--t1` | path | — | T1-weighted NIfTIs: a directory (all NIfTIs within) or one file. |
| `--fl`, `--flair` | path | — | FLAIR NIfTIs (directory or file). |
| `--t2` | path | — | T2 NIfTIs (directory or file). |
| `--t1ce` | path | — | T1CE NIfTIs (directory or file). |
| `--adc` | path | — | ADC NIfTIs (directory or file). |
| `--pet` | path | — | PET NIfTIs (directory or file). |
| `--image` | `MOD=PATH` (repeatable) | — | **Fallback** for a modality without a named flag above (e.g. a newly added one). Prefer the named flags when one exists. |
| `--participants` | path | — | Participants CSV (subject IDs + covariates). |
| `--existing` | flag | off | Add to an existing project instead of creating a new one. |
| `--param`, `-p` | `key=value` (repeatable) | — | Pipeline parameter. Typed `int → float → bool → str`. |
| `--reuse-cache / --no-reuse-cache` | flag | reuse | Reuse cached step outputs when inputs are unchanged. |
| `--wait-until-done / --no-wait` | flag | no-wait | Block and stream progress until the run finishes. |
| `--force` | flag | off | Submit even if the readiness check fails. |
| `--dry-run` | flag | off | Validate inputs and print the plan; create/upload/submit **nothing** (and never starts a server). |
| `--server` | `auto`\|`attach`\|`spawn` | `auto` | How to obtain a server (see [Server lifecycle](#server-lifecycle)). |
| `--keep-server` | flag | off | Don't shut down a server this command started. |
| `--server-log` | path | temp file | Where to write a spawned server's logs. |
| `--server-timeout` | int (s) | 1800 (0 if `--keep-server`) | Idle auto-shutdown for a spawned server; never fires while a run is in progress. `-1`/`0` disables. |

Global `--url` (or `NICHART_API_URL`) selects the target server, as with any command.

## Server lifecycle

`run` can operate against a server it starts itself, so you don't have to launch
one first. The governing rule is **ownership: only a server this command started
is ever shut down**; an already-running one is used and left untouched.

`--server` strategies:

| Value | Behavior |
|-------|----------|
| `auto` (default) | Attach to a running server at the target URL if one answers `/health`; otherwise start an ephemeral **local-mode** server on a free loopback port. |
| `attach` | Require a running server (the classic behavior). Error if none is reachable. |
| `spawn` | Always start a fresh local server, even if one is running. |

Details and guarantees:

- **Owned ⟹ waits.** A server we spawned also *runs* the pipeline, so `run` must
  block until the run is terminal before shutting it down. This **overrides
  `--no-wait`** — you'll be told when it happens. (An attached server keeps
  running independently, so `--no-wait` is honored there.)
- **Teardown** sends `SIGTERM` (graceful uvicorn shutdown), then `SIGKILL` if it
  doesn't exit. `--keep-server` leaves an owned server running and prints its URL
  and PID.
- **Local only.** Auto-start requires the target URL to be loopback. Pointing at a
  remote URL that's down is an error, not a reason to spawn a local server (a
  local server can't stand in for a remote one). Spawned servers always run in
  `NICHART_EXECUTION_MODE=local`.
- **Ctrl+C** while watching an owned server detaches *and* tears the server down,
  which stops the run (you're warned in the watch banner). Against an attached
  server, Ctrl+C just detaches and the run continues.
- **Startup failures** are surfaced: if the server doesn't become healthy within
  30 s, the tail of its log is printed and the command exits non-zero.
- **Idle auto-shutdown.** A spawned server self-terminates after `--server-timeout`
  seconds of no API activity — as a courtesy on shared systems, so an orphaned
  server (e.g. the CLI was killed before teardown) doesn't linger. The timer is
  **held off while any pipeline run is in progress** (including long SLURM/Batch
  jobs, whose run stays `running` for the whole job), so it never shuts down over
  active work; a full idle window elapses only after the last run finishes.
  Default 30 min; `--keep-server` turns it off; `-1`/`0` disables explicitly. A
  manually-launched server is unaffected unless you set
  `NICHART_INACTIVITY_TIMEOUT_SECONDS` (off by default there).

### Configuration of a spawned server

See **[INSTALLATION.md](../INSTALLATION.md)** for full install/config, including
building Singularity images (`scripts/build-sif-registry.py`) and the SLURM setup.

A spawned server is **not** a bare default server — it runs with the operator's
own configuration, resolved independently of your current directory:

- The `resources/` catalog ships inside the package and `.env` is resolved from
  the standard locations (`~/.nichart/.env`, a dev-checkout `<repo>/.env`, or
  `$NICHART_ENV_FILE`) — see INSTALLATION.md §4 — so invoking `nichart run` from
  any folder works. You configure once at install time; every `run` reuses it.
- **Config precedence:** inherited `NICHART_*` environment variables > those
  `.env` files > server defaults. `execution_mode` is forced to `local` (so the
  CLI can talk to the server without auth).

**Backend selection** follows the server's normal rules, driven by that config:

| Config | Spawned backend |
|--------|-----------------|
| `NICHART_JOB_BACKEND=docker` (or unset, no SIF dir) | Docker |
| `NICHART_JOB_BACKEND=singularity`, or `NICHART_SIF_DIR` set | Singularity |
| `NICHART_JOB_BACKEND=slurm` | SLURM |
| `NICHART_JOB_BACKEND=batch` | **refused** — Batch is cloud-only; attach to your deployed API instead (`--server attach`, `--url …`) |

The Docker prerequisite warning fires **only** when the effective backend is
Docker (and neither a `docker` CLI nor a docker socket is present) — it won't nag
Singularity/SLURM setups.

> In a shared computing environment this is the point: one configured install
> (its `.env` selecting SLURM/Singularity, data root, etc.) is reused by everyone
> who runs `nichart run`, so pipeline runs are reproducible without each user
> re-specifying the environment.

### Remote servers (not yet implemented)

A `--server remote` strategy is stubbed but not built. The plan is the "VS Code
remote" model: over SSH, ensure the NiChart server component is installed on the
host, start it bound to loopback there, open an SSH tunnel to a local port, and
attach through the tunnel — with the same ownership contract (an owned remote
server is torn down and the tunnel closed on exit). Only `_open_remote` in
[`cli.py`](cli.py) needs filling in; all the `run` logic above is transport-agnostic.

---

## Behavior details

### Modality inputs

- Common modalities have named flags: `--t1`, `--fl`, `--t2`, `--t1ce`, `--adc`,
  `--pet`. Any other registered modality (see `GET /catalog/modalities`) is given
  via the **fallback** `--image MOD=PATH` — prefer a named flag when one exists.
- A modality flag pointing at a **directory** uploads every `.nii`/`.nii.gz`
  file directly inside it (non-recursive), all tagged as that modality.
- Pointing at a **single file** uploads just that file.
- MRID inference strips known modality/type suffixes (`_T1`, `_FLAIR`, …) and
  extensions from the filename. If a file's MRID can't be derived, the run aborts
  before committing anything for that modality — rename the files, or upload them
  individually with `nichart files upload-nifti` to set MRIDs by hand.

### Subject-count mismatch diagnostics

After uploads, if more than one modality was provided and their subject counts
differ, you'll see, e.g.:

```
⚠ Subject counts differ across modalities:
    T1    3 subject(s)
    FL    2 subject(s)
    FL missing: sub-03
```

This is a **warning**, not a hard stop — some pipelines legitimately use one
modality. The readiness check that follows decides whether the run can proceed.

### Readiness gating

The readiness report is always rendered. If it is **not satisfied**:

- without `--force`: the run stops with a non-zero exit and a hint.
- with `--force`: it submits anyway (useful when you know better than the check).

If readiness is satisfied but below a *recommended* subject count (harmonized
pipelines), it warns but proceeds.

### Waiting vs. detaching

- Default (`--no-wait`): prints the run ID and the commands to track it
  (`nichart jobs <id>`, `nichart jobs logs <id>`, `nichart results show …`).
- `--wait-until-done`: streams a live per-step table until the run reaches a
  terminal state. Ctrl+C detaches your terminal; the run keeps going on the server.

### `--dry-run`

Validates the pipeline ID, resolves and counts the modality files, checks the
participants CSV exists, and prints the full plan — **without** creating a
project, uploading, or submitting. It also flags a would-be project collision (or
a missing project under `--existing`) so you catch it before doing real work.

---

## Exit codes

| Code | When |
|------|------|
| `0` | Submitted successfully, or a clean `--dry-run`. |
| `1` | Any validation, upload, readiness (without `--force`), or submission error. On a fresh project, the partial-data cleanup hint is printed first. |

---

## Examples

```bash
# Minimal: one modality, new project, don't wait.
nichart run run_dlmuse --project s1 --t1 /data/t1

# Preview only — no writes.
nichart run run_dlmuse --project s1 --t1 /data/t1 --dry-run

# Multi-modality + participants, block until finished.
nichart run run_spare_all --project s1 \
    --t1 /data/t1 --fl /data/flair \
    --participants participants.csv \
    --wait-until-done

# Add more data to an existing project and re-run with a parameter override.
nichart run dummy_pipeline --project s1 --existing \
    --param duration_seconds=30 --no-reuse-cache

# Point at a remote server.
nichart --url https://api.neuroimagingchart.com run run_dlmuse -P s1 --t1 ./t1
```
