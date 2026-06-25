"""
Response schemas for the pipeline readiness check endpoint.
"""

from pydantic import BaseModel, Field


class ImagingRequirement(BaseModel):
    """Readiness check for a single required imaging modality."""

    modality: str = Field(description="Imaging modality directory (t1, fl, t2, t1ce, adc).")
    subject_count: int = Field(description="Number of NIfTI files found in the modality directory.")
    satisfied: bool = Field(description="True if at least one subject's file is present.")


class ColumnCheck(BaseModel):
    """Readiness check for one required column in participants.csv."""

    column: str = Field(description="Required column name.")
    present: bool = Field(description="True if this column exists in participants.csv.")
    subjects_missing: list[str] = Field(
        default_factory=list,
        description="MRIDs of subjects where this column is empty or absent.",
    )
    subjects_invalid: list[str] = Field(
        default_factory=list,
        description=(
            "MRIDs of subjects where this column's value fails the pipeline's declared schema "
            "(wrong type, out of range, or not in allowed categorical values)."
        ),
    )


class CsvRequirement(BaseModel):
    """Aggregate readiness check for all required participants.csv columns."""

    required_columns: list[ColumnCheck] = Field(description="Per-column check results.")
    total_subjects: int = Field(description="Total subjects (rows) in participants.csv.")
    satisfied: bool = Field(
        description="True if every required column exists and is non-empty for all subjects."
    )


class SubjectCountRequirement(BaseModel):
    """Readiness check for a minimum subject count, used by harmonized pipelines."""

    actual: int = Field(description="Unique MRIDs detected across all modality directories.")
    required: int = Field(description="Minimum subjects needed to run the pipeline at all.")
    recommended: int = Field(description="Recommended number of subjects for reliable results.")
    satisfied: bool = Field(description="True if actual >= required.")
    recommended_met: bool = Field(description="True if actual >= recommended.")


class ReadinessReport(BaseModel):
    """Project readiness check result for a specific pipeline."""

    pipeline_id: str = Field(description="Pipeline that was checked.")
    satisfied: bool = Field(description="True if all hard requirements pass.")
    imaging: list[ImagingRequirement] = Field(
        default_factory=list,
        description="One entry per imaging modality required by the pipeline.",
    )
    csv: CsvRequirement | None = Field(
        default=None,
        description="CSV column checks, present only when the pipeline has csv_has_columns requirements.",
    )
    subject_count: SubjectCountRequirement | None = Field(
        default=None,
        description=(
            "Subject count check, present only for pipelines with min_subjects requirements "
            "(e.g. harmonized pipelines). satisfied=False blocks running; "
            "recommended_met=False should surface a warning to the user."
        ),
    )
