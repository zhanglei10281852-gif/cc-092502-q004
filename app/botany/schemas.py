from __future__ import annotations

from pydantic import BaseModel, Field


class ViewMapping(BaseModel):
    taxon_code: str = Field(..., min_length=1, max_length=80)
    mapped_code: str = Field(..., min_length=1, max_length=80)
    mapped_label: str = Field(default="", max_length=200)


class ViewCreate(BaseModel):
    view_code: str = Field(..., min_length=1, max_length=80)
    label: str = Field(default="", max_length=200)
    mappings: list[ViewMapping] = Field(default_factory=list)


class ComparisonRequest(BaseModel):
    view_id: int | None = None
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    iterations: int = Field(default=2000, ge=50, le=100_000)
    confidence: float = Field(default=0.95, ge=0.8, lt=1.0)
    include_polluted: bool = False
    context_types: list[str] | None = None
    label: str = Field(default="", max_length=200)


class ChangeItem(BaseModel):
    action: str = Field(..., pattern="^(rename|merge)$")
    taxon_code: str = Field(..., min_length=1, max_length=80)
    new_code: str | None = Field(default=None, max_length=80)
    new_name: str | None = Field(default=None, max_length=200)
    target_taxon_code: str | None = Field(default=None, max_length=80)
    note: str = Field(default="", max_length=500)


class ChangesetCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    rationale: str = Field(default="", max_length=1000)
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    iterations: int = Field(default=2000, ge=50, le=100_000)
    items: list[ChangeItem] = Field(..., min_length=1)


class ReviewRequest(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected)$")
    note: str = Field(default="", max_length=1000)
