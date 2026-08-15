"""Inspection + replay endpoints — the operator's window into the queue/DLQ."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Event, EventStatus, Source
from app.schemas import EventListResponse, EventOut, ReplayResponse

router = APIRouter(prefix="/v1", tags=["events"])
log = logging.getLogger("api.events")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/events/{event_id}", response_model=EventOut)
def get_event(event_id: uuid.UUID, session: Session = Depends(get_session)) -> Event:
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    return event


@router.get("/events", response_model=EventListResponse)
def list_events(
    status: EventStatus | None = Query(default=None, description="filter by status (e.g. dead)"),
    source: str | None = Query(default=None, description="filter by source name"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> EventListResponse:
    """List events, newest first. `?status=dead` is the dead-letter queue view."""
    stmt = select(Event)
    if source is not None:
        stmt = stmt.join(Source, Event.source_id == Source.id).where(Source.name == source)
    if status is not None:
        stmt = stmt.where(Event.status == status)
    stmt = stmt.order_by(Event.created_at.desc()).limit(limit).offset(offset)

    events = session.execute(stmt).scalars().all()
    return EventListResponse(
        count=len(events),
        limit=limit,
        offset=offset,
        events=[EventOut.model_validate(e) for e in events],
    )


@router.post("/events/{event_id}/replay", response_model=ReplayResponse)
def replay_event(event_id: uuid.UUID, session: Session = Depends(get_session)) -> ReplayResponse:
    """Re-queue a dead event: status -> pending, fresh attempt budget, due now.

    The transition is a single conditional UPDATE guarded by `status = 'dead'`,
    so two concurrent replays (or a replay racing anything else) can't double
    re-queue: only the first matching UPDATE flips the row, the rest no-op. If
    the event still passes the monotonic guard it will deliver; if a newer
    ordinal has since been delivered, the worker will (correctly) supersede it.
    """
    now = _now()
    updated = session.execute(
        update(Event)
        .where(Event.id == event_id, Event.status == EventStatus.dead)
        .values(
            status=EventStatus.pending,
            attempts=0,
            next_attempt_at=now,
            last_error=None,
            updated_at=now,
        )
        .returning(Event.id)
    ).first()
    session.commit()

    if updated is not None:
        log.info("event replayed", extra={"event_id": str(event_id)})
        return ReplayResponse(status="requeued", event_id=str(event_id))

    # Nothing updated: distinguish "not found" from "not in a replayable state".
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    raise HTTPException(
        status_code=409,
        detail=f"event is '{event.status.value}'; only dead events can be replayed",
    )
