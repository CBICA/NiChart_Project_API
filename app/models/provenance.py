"""Response schemas for the provenance verification endpoint."""

from typing import Literal

from pydantic import BaseModel, Field


class ProvenanceInputCheck(BaseModel):
    """Staleness check for one input path recorded in a provenance file."""

    label: str = Field(description="Mount label as declared in the tool YAML.")
    path: str = Field(description="Absolute host path recorded at run time.")
    status: Literal["clean", "modified", "missing"] = Field(
        description=(
            "'clean' — no files changed after the step finished. "
            "'modified' — at least one file was written after the step's generated_at timestamp. "
            "'missing' — the path no longer exists."
        )
    )
    modified_count: int = Field(
        default=0,
        description="Number of files within the path that have been modified. "
                    "Zero for 'clean' or 'missing' entries.",
    )


class ProvenanceEntry(BaseModel):
    """Parsed and verified contents of one _provenance.json file."""

    output_dir: str = Field(
        description="Project-relative path of the directory containing this _provenance.json."
    )
    pipeline_id: str = Field(description="Pipeline that produced this output.")
    step_id: str = Field(description="Step within that pipeline.")
    container_image: str = Field(description="Container image used.")
    generated_at: str = Field(description="ISO timestamp when the step finished.")
    execution_mode: str = Field(
        default="",
        description="Execution mode at time of run: 'local' or 'cloud'.",
    )
    user_id: str = Field(
        default="",
        description="Cognito sub (cloud) or local user identifier of the user who ran the step.",
    )
    backend: str = Field(
        default="",
        description="Job backend used: 'docker', 'singularity', 'slurm', or 'batch'.",
    )
    inputs: list[ProvenanceInputCheck] = Field(
        description="Staleness check for each input path recorded at run time."
    )
    overall: Literal["clean", "dirty", "missing_inputs", "unreadable"] = Field(
        description=(
            "'clean' — all inputs unchanged since the step ran. "
            "'dirty' — at least one input was modified after the step. "
            "'missing_inputs' — at least one input path no longer exists. "
            "'unreadable' — the _provenance.json file could not be parsed."
        )
    )
    error: str | None = Field(
        default=None,
        description="Parse error message, present only when overall='unreadable'.",
    )


class ProvenanceReport(BaseModel):
    """Provenance verification report for a project."""

    project_id: str = Field(description="Project that was scanned.")
    entries: list[ProvenanceEntry] = Field(
        description="One entry per _provenance.json file found in the project tree."
    )
    summary: Literal["all_clean", "some_dirty", "no_provenance"] = Field(
        description=(
            "'all_clean' — every step's inputs are unchanged. "
            "'some_dirty' — at least one step has stale or missing inputs. "
            "'no_provenance' — no _provenance.json files found (no steps have completed yet)."
        )
    )


# ── Provenance report (GET /projects/{id}/provenance/report) ──────────────────

class DatasetDigest(BaseModel):
    """A file or directory's content, summarised."""

    path: str = Field(description="Project-relative path.")
    files: int = Field(description="Number of files (excluding _provenance.json).")
    bytes: int = Field(description="Total size in bytes.")
    digest: str = Field(
        description=(
            "SHA-256 over the sorted '<sha256>  <path>' lines of its files, paths relative "
            "to this path. Equal digests mean identical content."
        )
    )


class InputDataset(DatasetDigest):
    """One raw-input root of the project (a modality's images, participants, IDAT)."""

    label: str = Field(description="Human-readable name, e.g. 'T1-weighted images'.")
    untracked_files: int = Field(
        default=0,
        description=(
            "Files with no upload record: present before upload tracking began, or "
            "changed outside an upload. Their times are file modification times."
        ),
    )
    first_recorded_at: str | None = Field(
        default=None,
        description=(
            "Earliest event touching it — or, when that is the baseline, the oldest "
            "modification time of its files (see first_recorded_from_mtime)."
        ),
    )
    first_recorded_from_mtime: bool = Field(
        default=False,
        description="True when first_recorded_at is a file modification time, not an upload record.",
    )
    last_changed_at: str | None = Field(default=None, description="Latest event touching it.")


class IngestEventOut(BaseModel):
    """One change to the project's raw inputs (an upload, an edit, a deletion)."""

    event_id: str
    kind: str = Field(
        description=(
            "'nifti_upload', 'dicom_conversion', 'bids_upload', 'idat_upload', "
            "'participants_upload', 'participants_edit', 'file_delete', 'subjects_delete', "
            "'baseline' (inputs already present when tracking began) or 'untracked_change' "
            "(a change made outside any tracked upload)."
        )
    )
    at: str = Field(description="When it was recorded (ISO 8601, UTC).")
    roots: list[str] = Field(description="Input roots it touched, e.g. ['t1', 'participants'].")
    added: int
    changed: int
    removed: int
    files: list[str] = Field(description="Paths it added or changed (first 50).")
    detail: dict = Field(default_factory=dict, description="Kind-specific detail (filename, run_id, ...).")
    image: str | None = Field(default=None, description="Converter image, for a DICOM conversion.")
    earliest_mtime: str | None = Field(
        default=None, description="Baseline only: oldest modification time among its files."
    )
    latest_mtime: str | None = Field(
        default=None, description="Baseline only: newest modification time among its files."
    )


class InputSource(BaseModel):
    """Where one input of a step comes from."""

    label: str = Field(description="Mount label in the tool definition.")
    path: str = Field(description="Project-relative path (absolute if outside the project).")
    kind: Literal["input", "step", "manual", "external", "unknown"] = Field(
        description=(
            "'input' — raw project input (see ref for the root); 'step' — output of another "
            "step (ref is its execution_id); 'manual' — manually uploaded intermediate data; "
            "'external' — outside the project (e.g. a server resource); 'unknown' — no record."
        )
    )
    ref: str | None = None


class ExecutionOut(BaseModel):
    """One executed pipeline step that produced current project outputs."""

    execution_id: str = Field(description="'<run_id>:<step_id>'.")
    run_id: str | None
    pipeline_id: str
    step_id: str
    step_name: str | None = None
    tool_id: str | None
    tool_name: str | None = None
    image: str
    image_digest: str | None = Field(default=None, description="repo@sha256:..., when recorded.")
    backend: str = ""
    execution_mode: str = ""
    submitted_at: str | None = None
    finished_at: str | None = None
    params: dict = Field(default_factory=dict, description="Resolved tool parameters.")
    command: str | None = Field(default=None, description="Rendered container command.")
    inputs: list[InputSource]
    outputs: list[DatasetDigest] = Field(description="Its outputs as they are now.")
    depends_on: list[str] = Field(description="execution_ids whose outputs it read.")
    chunks: int = Field(default=0, description="Parallel subject chunks it ran as (0 = one job).")
    reconstructed: bool = Field(
        description=(
            "True when recorded before full provenance tracking: command and mounts are "
            "rebuilt from the current tool definition and the image from _provenance.json."
        )
    )
    warnings: list[str] = Field(default_factory=list)
    commands: dict[str, str] | None = Field(
        default=None,
        description=(
            "Shell commands to rerun it, keyed by backend ('docker', 'singularity'), with "
            "paths under $NICHART_PROJECT. Null when the tool definition is unavailable."
        ),
    )
    results: list[str] = Field(default_factory=list, description="pipeline_ids of results it feeds.")


class ResultLineage(BaseModel):
    """A pipeline result present in the project, and the steps behind it."""

    pipeline_id: str
    pipeline_name: str
    paths: list[str] = Field(description="Its result files/directories (project-relative).")
    path_notes: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "For result paths written by an earlier step rather than the one that makes the "
            "result this pipeline's own: where they really come from, e.g. 'From DLMUSE — "
            "not harmonized'."
        ),
    )
    executions: list[str] = Field(description="execution_ids behind it, in run order.")
    inputs: list[str] = Field(description="Raw input roots it derives from.")
    warnings: list[str] = Field(default_factory=list)


class ProvenanceDossier(BaseModel):
    """How every current result of a project was produced, and how to reproduce it."""

    project_id: str
    generated_at: str
    api_version: str
    status: Literal["ok", "warnings", "empty"] = Field(
        description="'empty' — no results yet; 'warnings' — see warnings; else 'ok'."
    )
    warnings: list[str] = Field(default_factory=list)
    inputs: list[InputDataset]
    events: list[IngestEventOut] = Field(description="Changes to raw inputs, oldest first.")
    executions: list[ExecutionOut] = Field(description="In an order that respects dependencies.")
    results: list[ResultLineage]
    setup: dict[str, str] = Field(description="Image pulls per backend.")
    notes: list[str] = Field(default_factory=list, description="Caveats for anyone replicating.")
