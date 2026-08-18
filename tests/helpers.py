"""Shared helpers for tests."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.db import SessionLocal
from app.models import Event, EventStatus, Source
from app.security import compute_signature


def sig_header(secret: str, body: bytes) -> str:
    return "sha256=" + compute_signature(secret, body)


def add_source(name: str, secret: str = "s3cr3t", url: str = "http://downstream.test/hook") -> int:
    session = SessionLocal()
    try:
        src = Source(name=name, signing_secret=secret, downstream_url=url)
        session.add(src)
        session.commit()
        return src.id
    finally:
        session.close()


def add_event(source_id: int, pid: str, resource: str, ordinal: int) -> str:
    session = SessionLocal()
    try:
        ev = Event(
            source_id=source_id,
            provider_event_id=pid,
            resource_key=resource,
            status_ordinal=ordinal,
            payload={"pid": pid},
            next_attempt_at=datetime.now(timezone.utc),
        )
        session.add(ev)
        session.commit()
        return str(ev.id)
    finally:
        session.close()


def event_status(event_id: str) -> EventStatus:
    session = SessionLocal()
    try:
        return session.get(Event, event_id).status
    finally:
        session.close()


def event_row(event_id: str) -> Event:
    session = SessionLocal()
    try:
        ev = session.get(Event, event_id)
        session.expunge(ev)
        return ev
    finally:
        session.close()


def count_events(source_id: int, provider_event_id: str) -> int:
    session = SessionLocal()
    try:
        return (
            session.query(Event)
            .filter_by(source_id=source_id, provider_event_id=provider_event_id)
            .count()
        )
    finally:
        session.close()


def mock_client(status_code: int) -> httpx.Client:
    """An httpx.Client whose every request returns `status_code` (no network)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"ok": status_code < 300})

    return httpx.Client(transport=httpx.MockTransport(handler))
