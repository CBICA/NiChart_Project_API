"""Response schemas for the documentation endpoints."""

from typing import Literal

from pydantic import BaseModel, Field


class DocSection(BaseModel):
    """A single section within a documentation topic."""

    id: str = Field(description="Unique identifier for this section within the topic.")
    title: str = Field(description="Display title for this section (e.g. tab label).")
    file: str = Field(
        description=(
            "Filename relative to the topic folder. "
            "Fetch the content with GET /catalog/docs/{docs_id}/{file}."
        ),
    )
    audience: Literal["user", "developer", "all"] = Field(
        default="all",
        description=(
            "'user' — shown in the NiChart end-user interface. "
            "'developer' — shown in developer/technical documentation. "
            "'all' — shown in both contexts."
        ),
    )
    type: Literal["markdown", "data", "image"] = Field(
        default="markdown",
        description=(
            "'markdown' — render as prose with react-markdown or equivalent. "
            "'data' — parse as JSON for plot/chart rendering. "
            "'image' — display as a static image."
        ),
    )


class DocManifest(BaseModel):
    """Full documentation manifest for a single topic."""

    docs_id: str = Field(description="Topic identifier (folder name under resources/docs/).")
    title: str = Field(description="Human-readable topic title.")
    description: str | None = Field(
        default=None,
        description="One-sentence summary shown in the docs index.",
    )
    pipelines: list[str] = Field(
        default_factory=list,
        description=(
            "Pipeline IDs covered by this documentation topic. "
            "Multiple pipelines (e.g. harmonized and base variants) can share one topic."
        ),
    )
    thumbnail: str | None = Field(
        default=None,
        description=(
            "Filename of a thumbnail image relative to the topic folder. "
            "Fetch with GET /catalog/docs/{docs_id}/{thumbnail}."
        ),
    )
    sections: list[DocSection] = Field(
        default_factory=list,
        description="Ordered list of content sections available for this topic.",
    )


class DocTopicSummary(BaseModel):
    """Abbreviated documentation topic for the index listing."""

    docs_id: str = Field(description="Topic identifier.")
    title: str = Field(description="Human-readable topic title.")
    description: str | None = Field(default=None)
    pipelines: list[str] = Field(
        default_factory=list,
        description="Pipeline IDs covered by this topic.",
    )
    thumbnail: str | None = Field(
        default=None,
        description=(
            "Filename of a thumbnail image relative to the topic folder. "
            "Fetch with GET /catalog/docs/{docs_id}/{thumbnail}."
        ),
    )
