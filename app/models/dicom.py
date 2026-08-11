"""
Request/response schemas for DICOM staging and conversion endpoints.
"""

from pydantic import BaseModel, Field, field_validator


class DicomStagingResult(BaseModel):
    """Response after uploading a DICOM zip."""

    staging_id: str = Field(description="Opaque identifier for this DICOM staging area.")


class SeriesInfo(BaseModel):
    """Metadata for a single DICOM series detected in a staged upload."""

    series_uid: str = Field(description="DICOM SeriesInstanceUID.")
    series_description: str | None = Field(default=None, description="DICOM SeriesDescription tag.")
    modality: str | None = Field(default=None, description="DICOM Modality tag (e.g. 'MR', 'CT').")
    study_date: str | None = Field(default=None, description="DICOM StudyDate tag (YYYYMMDD).")
    patient_id: str | None = Field(default=None, description="DICOM PatientID tag.")
    num_files: int = Field(description="Number of DICOM files belonging to this series.")


class DicomSeriesListing(BaseModel):
    """All DICOM series detected in a staged upload."""

    staging_id: str
    series: list[SeriesInfo]


class SeriesMapping(BaseModel):
    """User-confirmed mapping from a DICOM series to a NiChart modality."""

    series_uid: str = Field(description="SeriesInstanceUID of the series to convert.")
    nichart_modality: str = Field(
        description="Target NiChart modality code (see GET /catalog/modalities)."
    )

    @field_validator("nichart_modality")
    @classmethod
    def _known_modality(cls, v: str) -> str:
        from app import modalities

        if not modalities.is_valid(v):
            raise ValueError(f"Unknown modality {v!r}. Valid: {list(modalities.MODALITY_CODES)}")
        return v
    mrid: str | None = Field(
        default=None,
        description=(
            "Output filename prefix (MRID). Defaults to the DICOM PatientID if not provided."
        ),
    )


class DicomConvertRequest(BaseModel):
    """Request body for the DICOM conversion endpoint."""

    series_mappings: list[SeriesMapping] = Field(
        description="One entry per series to convert. Series not listed are ignored."
    )


class DicomConvertResult(BaseModel):
    """Response after a conversion job is successfully submitted."""

    run_id: str = Field(description="Job run ID. Poll /jobs/pipelines/{run_id} for status.")
