"""
Response schemas for the pipeline readiness check endpoint.
"""

from pydantic import BaseModel, Field


class ImagingRequirement(BaseModel):
    """Readiness check for a single required imaging modality."""

    modality: str = Field(description="Imaging modality directory (t1, fl, t2, t1ce, adc).")
    subject_count: int = Field(description="Number of NIfTI files found in the modality directory.")
    mrids: list[str] = Field(
        default_factory=list,
        description="MRIDs (filename stems) found in this modality directory.",
    )
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


class CompleteSetsRequirement(BaseModel):
    """Cross-modality check: each subject must have files in every required modality."""

    required_modalities: list[str] = Field(
        description="Modalities that must all be present for a subject to be considered complete."
    )
    complete_mrids: list[str] = Field(
        description="MRIDs that have files in every required modality."
    )
    incomplete_mrids: dict[str, list[str]] = Field(
        description=(
            "MRIDs that are missing at least one modality. "
            "Maps MRID → list of modalities it is missing."
        )
    )
    complete_count: int = Field(description="Number of subjects with a complete set of modalities.")
    satisfied: bool = Field(description="True if at least one subject has a complete set.")


class IdatRequirement(BaseModel):
    """Readiness check for paired IDAT files ({MRID}_Red.idat + {MRID}_Grn.idat)."""

    complete_mrids: list[str] = Field(
        description="MRIDs with both _Red.idat and _Grn.idat present."
    )
    missing_red: list[str] = Field(
        description="MRIDs that have _Grn.idat but are missing _Red.idat."
    )
    missing_grn: list[str] = Field(
        description="MRIDs that have _Red.idat but are missing _Grn.idat."
    )
    complete_count: int = Field(description="Number of MRIDs with both files present.")
    satisfied: bool = Field(description="True if at least one complete MRID pair exists.")


class ReadinessReport(BaseModel):
    """Project readiness check result for a specific pipeline."""

    pipeline_id: str = Field(description="Pipeline that was checked.")
    satisfied: bool = Field(description="True if all hard requirements pass.")
    imaging: list[ImagingRequirement] = Field(
        default_factory=list,
        description="One entry per imaging modality required by the pipeline.",
    )
    complete_sets: CompleteSetsRequirement | None = Field(
        default=None,
        description=(
            "Cross-modality completeness check. Present when the pipeline requires two or more "
            "imaging modalities. Reports which subjects have a full complement of required images."
        ),
    )
    idat: IdatRequirement | None = Field(
        default=None,
        description="IDAT paired-file check. Present when the pipeline has a needs_idat requirement.",
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
