"""Core ingest logic: extract identifying fields and insert idempotently.

Kept separate from the HTTP router so the insert path can be unit-tested and
reused (e.g. by the concurrency test that fires two identical inserts at once).
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Event


class IngestError(Exception):
    """Raised for a malformed but authenticated payload -> HTTP 400."""


def extract_event_fields(payload: dict[str, Any], event_id_header: str | None) -> tuple[str, str, int]:
    """Pull (provider_event_id, resource_key, status_ordinal) from a webhook.

    Contract (documented in README/WALKTHROUGH):
      - provider_event_id: `X-Event-Id` header, else payload["event_id"], else
        payload["id"]. REQUIRED — without a stable id we cannot dedupe, so we
        reject rather than silently invent one.
      - resource_key: payload["resource_key"], else the provider_event_id (an
        event is its own resource unless it declares a shared one).
      - status_ordinal: payload["status_ordinal"], else 0.
    """
    provider_event_id = event_id_header or payload.get("event_id") or payload.get("id")
    if not provider_event_id:
        raise IngestError("missing event id (X-Event-Id header or 'event_id'/'id' in body)")

    resource_key = payload.get("resource_key") or str(provider_event_id)

    raw_ordinal = payload.get("status_ordinal", 0)
    try:
        status_ordinal = int(raw_ordinal)
    except (TypeError, ValueError):
        raise IngestError("status_ordinal must be an integer")

    return str(provider_event_id), str(resource_key), status_ordinal


def insert_event_idempotent(
    session: Session,
    *,
    source_id: int,
    provider_event_id: str,
    resource_key: str,
    status_ordinal: int,
    payload: dict[str, Any],
) -> tuple[str, bool]:
    """Insert one event, or no-op if it already exists. Returns (event_id, inserted).

    Idempotency is enforced by the database via
    `INSERT ... ON CONFLICT (source_id, provider_event_id) DO NOTHING`, NOT by a
    read-then-write in Python. Two identical requests racing in parallel would
    both pass a "does it exist?" check and both insert; the unique index makes
    the second insert a no-op instead, so exactly one row survives no matter the
    interleaving. RETURNING tells us which request actually created the row.
    """
    new_id = uuid.uuid4()
    stmt = (
        pg_insert(Event)
        .values(
            id=new_id,
            source_id=source_id,
            provider_event_id=provider_event_id,
            resource_key=resource_key,
            status_ordinal=status_ordinal,
            payload=payload,
        )
        .on_conflict_do_nothing(constraint="uq_events_source_provider")
        .returning(Event.id)
    )
    row = session.execute(stmt).first()
    session.commit()

    if row is not None:
        # We won the race / it was new.
        return str(row[0]), True

    # Conflict: the row already exists. Fetch its id to return to the caller.
    existing_id = (
        session.query(Event.id)
        .filter(Event.source_id == source_id, Event.provider_event_id == provider_event_id)
        .scalar()
    )
    return str(existing_id), False
