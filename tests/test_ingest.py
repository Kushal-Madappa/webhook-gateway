"""Ingest endpoint: signature verification and idempotency."""
import json

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import add_source, count_events, sig_header

client = TestClient(app)


def _body(pid: str, ordinal: int = 1) -> bytes:
    return json.dumps({"event_id": pid, "resource_key": f"r:{pid}", "status_ordinal": ordinal}).encode()


def test_bad_signature_returns_401():
    add_source("acme", secret="right")
    body = _body("e1")
    resp = client.post("/v1/webhooks/acme", content=body, headers={"X-Signature": "sha256=deadbeef"})
    assert resp.status_code == 401


def test_unknown_source_returns_404():
    body = _body("e1")
    resp = client.post("/v1/webhooks/ghost", content=body, headers={"X-Signature": sig_header("x", body)})
    assert resp.status_code == 404


def test_valid_event_accepted():
    add_source("acme", secret="right")
    body = _body("ok-1")
    resp = client.post("/v1/webhooks/acme", content=body, headers={"X-Signature": sig_header("right", body)})
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_duplicate_creates_one_row():
    source_id = add_source("acme", secret="right")
    body = _body("dup-1")
    headers = {"X-Signature": sig_header("right", body)}

    r1 = client.post("/v1/webhooks/acme", content=body, headers=headers)
    r2 = client.post("/v1/webhooks/acme", content=body, headers=headers)

    assert r1.status_code == 200 and r1.json()["status"] == "accepted"
    assert r2.status_code == 200 and r2.json()["status"] == "duplicate"
    # Same surviving row, and exactly one row in the table.
    assert r1.json()["event_id"] == r2.json()["event_id"]
    assert count_events(source_id, "dup-1") == 1
