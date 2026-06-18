from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """Standard error response body."""

    detail: str = Field(description="Human-readable description of the error.")
