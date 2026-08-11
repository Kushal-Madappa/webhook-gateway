"""Webhook ingest endpoint.

Flow: look up source -> verify HMAC over raw body -> parse -> idempotent insert
-> COMMIT -> 200. The commit-before-200 ordering is the whole point (see below).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.db import SessionLocal
from app.ingest import IngestError, extract_event_fields, insert_event_idempotent
from app.models import Source
from app.security import verify_signature

router = APIRouter(prefix="/v1", tags=["webhooks"])


def _ingest_sync(source_name: str, raw_body: bytes, signature: str | None, event_id_header: str | None):
    """Synchronous DB work, run in a threadpool so it never blocks the loop."""
    session = SessionLocal()
    try:
        source = session.query(Source).filter(Source.name == source_name).one_or_none()
        # Unknown source -> 404. We must resolve the source first because its
        # signing_secret is what we verify the signature against.
        if source is None:
            return JSONResponse(status_code=404, content={"detail": "unknown source"})

        # Bad/missing signature -> 401. Rejected BEFORE we parse or touch the DB,
        # so an unauthenticated caller cannot probe our JSON handling.
        if not verify_signature(source.signing_secret, raw_body, signature):
            return JSONResponse(status_code=401, content={"detail": "invalid signature"})

        try:
            payload = json.loads(raw_body)
            if not isinstance(payload, dict):
                raise IngestError("payload must be a JSON object")
            provider_event_id, resource_key, status_ordinal = extract_event_fields(
                payload, event_id_header
            )
        except (json.JSONDecodeError, IngestError) as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})

        event_id, inserted = insert_event_idempotent(
            session,
            source_id=source.id,
            provider_event_id=provider_event_id,
            resource_key=resource_key,
            status_ordinal=status_ordinal,
            payload=payload,
        )
        # The row is committed by insert_event_idempotent BEFORE we return 200.
        # Providers treat a 200 as "you own this now, stop retrying"; if we ACKed
        # before durably persisting, a crash in that window would lose an event
        # the provider will never resend. Persist-then-ACK makes 200 an honest
        # promise that the event is safe in Postgres.
        return JSONResponse(
            status_code=200,
            content={"status": "accepted" if inserted else "duplicate", "event_id": event_id},
        )
    finally:
        session.close()


@router.post("/webhooks/{source}")
async def ingest(source: str, request: Request):
    # Read the RAW bytes: HMAC must be computed over exactly what was sent.
    raw_body = await request.body()
    signature = request.headers.get("x-signature")
    event_id_header = request.headers.get("x-event-id")
    return await run_in_threadpool(_ingest_sync, source, raw_body, signature, event_id_header)
