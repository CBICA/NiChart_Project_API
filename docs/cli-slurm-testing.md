# Testing the CLI on a SLURM cluster

A runnable checklist for validating the `nichart` CLI — especially the
auto-server-spawn feature — against the SLURM backend, independently of the
Docker/compose dev setup.

Install and configuration are covered in `INSTALLATION.md`; this doc assumes
you've read it. CLI reference: `app/CLI.md` and `app/CLI_run.md`.

---

## What you're validating

1. The CLI installs and runs standalone (editable install; entry point on PATH).
2. `nichart run` can **spawn a server itself** — and does so from the repo root, so
   it uses the operator's `.env` and `resources/`.
3. Spawn behaves correctly **env-free** (defaults, warns) and **env-ful** (uses your
   SLURM `.env`).
4. Jobs actually execute on the **SLURM executor** (via `sbatch` → Apptainer).
5. Ownership, waiting, and teardown semantics hold.

---

## Phase 0 — Install & prerequisites (submit host)

```bash
git clone … && cd NiChart_Project_API
python -m venv .venv && source .venv/bin/activate
pip install -e .                              # editable — REQUIRED (see INSTALLATION.md §3)

which nichart                                 # entry point present
python -c "import app.cli; print(app.cli.REPO_ROOT)"   # → repo root w/ .env + resources/
which sbatch squeue sacct                     # SLURM CLIs reachable
env | grep NICHART_ || echo "clean shell"     # no stray overrides
```

- Build the SIF registry to shared storage (see INSTALLATION.md §5 — likely via
  `sbatch build-sifs.sbatch` on your cluster): `ls "$NICHART_SIF_DIR"/*.sif`.
- Confirm `NICHART_DATA_ROOT` and `NICHART_SLURM_LOGS_DIR` are on a filesystem the
  compute nodes can read/write.

---

## Phase 1 — CLI sanity (no server)

```bash
nichart --help
nichart run --help                            # server flags present
nichart status                                # → cannot connect (nothing running) — expected
nichart run dummy_pipeline -P t0 --dry-run    # local validation, NO spawn; notes it *would* spawn
```

Expected: `--dry-run` never starts a server; it validates local inputs and, if no
server is reachable, says the real run would spawn one.

---

## Phase 2 — Spawn mechanics, ENV-FREE (proves the failure mode)

With **no `.env`** at the repo root and a clean shell:

```bash
mv .env .env.bak 2>/dev/null || true
nichart run dummy_pipeline -P envfree --server spawn
```

Expected:
- `No .env found at …` warning.
- Server spawns from the repo root, using defaults → backend resolves to **docker**.
- On an HPC node with no Docker → the Docker-prerequisite warning fires and the
  step **fails**. This is the intended, informative failure: it shows *why* the
  `.env` (selecting SLURM) is needed.

Also confirm the guard:

```bash
NICHART_JOB_BACKEND=batch nichart run dummy_pipeline -P b --server spawn
# → refused cleanly: Batch is cloud-only; use --server attach
```

Restore: `mv .env.bak .env`.

---

## Phase 3 — Spawn with the SLURM `.env` (the main event)

Repo-root `.env` (from INSTALLATION.md §4) selecting SLURM. Then:

```bash
nichart run dummy_pipeline -P slurm1 --server spawn
```

Expected:
- **No** Docker warning (backend = slurm).
- `Started a local API server (pid …) at http://127.0.0.1:<port>`.
- The step is submitted with `sbatch`. In another shell while it runs:
  ```bash
  squeue -u "$USER"                           # the NiChart job appears
  ```
- Because the CLI owns the server, it **waits** for the job to reach a terminal
  state (overriding `--no-wait`), then tears the server down.
- Verify outputs under `NICHART_DATA_ROOT`, and:
  ```bash
  ls "$NICHART_SLURM_LOGS_DIR"                 # a {job_id}.log appears
  ```

Then repeat with a **real** pipeline and real inputs to confirm Apptainer actually
executes the tool SIF on the compute node:

```bash
nichart run run_dlmuse --project slurm2 --t1 /shared/data/t1 --wait-until-done
nichart results show slurm2 run_dlmuse
```

Capture server-side detail if anything misbehaves:

```bash
nichart run … --server spawn --server-log /tmp/nichart-srv.log
# then inspect /tmp/nichart-srv.log (uvicorn + backend errors)
```

---

## Phase 4 — Persistent server + attach (for long / queued jobs)

Owned-server runs **block the terminal** until the SLURM job finishes — poor for
jobs that queue for hours. Test the detach path:

```bash
# From the repo root, under tmux/systemd, with your SLURM .env in effect:
uvicorn app.main:app --host 127.0.0.1 --port 8000 &
export NICHART_API_URL=http://127.0.0.1:8000

nichart run run_dlmuse -P slurm3 --t1 /shared/data/t1 --server attach --no-wait   # returns immediately
nichart jobs <run_id>            # poll later
nichart jobs logs <run_id>       # live SLURM log tail while running
nichart status                   # server still up — attach never kills a server it didn't start
```

The SLURM backend can reconnect to a running job after a server restart, so a
persistent server survives redeploys — good for a shared submit host.

---

## Phase 5 — Full data round-trip & diagnostics

```bash
nichart run run_dlmuse --project study --t1 /shared/data/t1 --fl /shared/data/flair \
    --participants participants.csv --wait-until-done
nichart results show study run_dlmuse
nichart files download study <output_path> -o ./out

# Subject-count mismatch diagnostic: give T1 and FL different subject sets and
# confirm the run reports which MRIDs are missing from which modality.
```

---

## Phase 6 — Edge cases

- Unknown pipeline → clear error + `pipelines list` hint.
- Project collision → error; `--existing` → adds to it.
- Readiness failure → blocked; `--force` → submits anyway.
- Un-inferable MRID → aborts and lists the offending files.
- **Ctrl+C during an owned-server wait** → server is torn down and the run stops
  (you're warned in the watch banner).
- Bad `.env` / server won't start → 30 s health timeout prints the server log tail.
- `--keep-server` → an owned server is left running (URL + PID printed).

---

## Gotchas (the ones that actually bite)

| Symptom | Cause / fix |
|---------|-------------|
| Empty catalog / "unknown pipeline" on a spawned server | Not an editable install → `REPO_ROOT` points into `site-packages` (no `.env`/`resources/`). `pip install -e .`. |
| `.env` seems ignored | A `NICHART_*` var exported in your shell overrides it. `env \| grep NICHART_`. |
| Docker warning on a SLURM box | Backend resolved to `docker` — your `.env`/env isn't selecting `slurm` (or is masked). |
| Job stuck / can't read inputs / no logs | `NICHART_DATA_ROOT` or `NICHART_SLURM_LOGS_DIR` not on a filesystem shared with compute nodes. |
| Apptainer "image not found" on the node | `NICHART_SIF_DIR` missing the tool's `.sif`, or not readable from compute nodes. Rebuild (INSTALLATION.md §5). |
| Everyone's data mixed together | A shared local-mode server treats all clients as the OS user running it. Use per-user ephemeral spawn (default) for isolation. |
| `apptainer build` fails on login node | Build the SIF registry as a SLURM job (INSTALLATION.md §5). |
