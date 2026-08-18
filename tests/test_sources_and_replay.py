"""Admin source registration and dead-letter replay."""
import json

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Event, EventStatus
from tests.helpers import add_event, add_source

client = TestClient(app)
ADMIN = {"Authorization": "Bearer test-admin-token"}


def test_create_source_requires_admin_token():
    body = {"name": "x", "signing_secret": "s", "downstream_url": "http://x"}
    assert client.post("/v1/sources", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/v1/sources", json=body, headers=ADMIN).status_code == 201


def _force_dead(event_id: str) -> None:
    session = SessionLocal()
    try:
        ev = session.get(Event, event_id)
        ev.status = EventStatus.dead
        ev.attempts = 3
        session.commit()
    finally:
        session.close()


def test_replay_only_dead_events():
    source_id = add_source("s1")
    event_id = add_event(source_id, "p1", "r:p1", 1)

    # A pending event cannot be replayed.
    assert client.post(f"/v1/events/{event_id}/replay").status_code == 409

    _force_dead(event_id)
    resp = client.post(f"/v1/events/{event_id}/replay")
    assert resp.status_code == 200 and resp.json()["status"] == "requeued"

    session = SessionLocal()
    try:
        ev = session.get(Event, event_id)
        assert ev.status == EventStatus.pending
        assert ev.attempts == 0
    finally:
        session.close()


def test_replay_missing_event_404():
    assert client.post("/v1/events/00000000-0000-0000-0000-000000000000/replay").status_code == 404


def test_list_filters_by_status_and_source():
    sid = add_source("filt")
    a = add_event(sid, "a", "r:a", 1)
    _force_dead(a)
    add_event(sid, "b", "r:b", 1)  # stays pending

    dead = client.get("/v1/events", params={"status": "dead"}).json()
    assert dead["count"] == 1 and dead["events"][0]["provider_event_id"] == "a"

    by_source = client.get("/v1/events", params={"source": "filt"}).json()
    assert by_source["count"] == 2
