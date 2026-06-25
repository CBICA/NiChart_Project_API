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
