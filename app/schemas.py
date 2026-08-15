"""Pydantic request/response models."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import EventStatus


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


class EventOut(BaseModel):
    """Full event record (for GET /v1/events/{id} and list rows)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_id: int
    provider_event_id: str
    resource_key: str
    status_ordinal: int
    status: EventStatus
    attempts: int
    payload: dict
    next_attempt_at: datetime
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class EventListResponse(BaseModel):
    count: int
    limit: int
    offset: int
    events: list[EventOut]


class ReplayResponse(BaseModel):
    status: str  # "requeued"
    event_id: str
