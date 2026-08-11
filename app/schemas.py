"""Pydantic request/response models."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SourceCreate(BaseModel):
    name: str
    signing_secret: str
    downstream_url: str


class SourceOut(BaseModel):
    # signing_secret is deliberately NOT exposed in responses.
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    downstream_url: str
    created_at: datetime


class IngestResponse(BaseModel):
    status: str  # "accepted" | "duplicate"
    event_id: str
