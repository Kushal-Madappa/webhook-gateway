"""Worker: delivery, retry->dead, and the monotonic ordering guard."""
from app.db import SessionLocal
from app.models import EventStatus
from app.worker import process_one
from tests.helpers import add_event, add_source, event_row, event_status, mock_client


def _process_one(client) -> None:
    session = SessionLocal()
    try:
        process_one(session, client)
    finally:
        session.close()


def test_success_delivers():
    source_id = add_source("good", url="http://good.test/hook")
    event_id = add_event(source_id, "g1", "r:g1", 1)

    _process_one(mock_client(200))

    assert event_status(event_id) == EventStatus.delivered


def test_downstream_500_retried_then_dead():
    # conftest sets MAX_ATTEMPTS=3 and zero backoff.
    source_id = add_source("bad", url="http://bad.test/hook")
    event_id = add_event(source_id, "d1", "r:d1", 1)
    client = mock_client(500)

    for _ in range(10):
        _process_one(client)
        if event_status(event_id) in (EventStatus.dead, EventStatus.delivered):
            break

    row = event_row(event_id)
    assert row.status == EventStatus.dead
    assert row.attempts == 3
    assert "500" in (row.last_error or "")


def test_out_of_order_stale_dropped():
    source_id = add_source("ooo", url="http://ooo.test/hook")
    client = mock_client(200)

    # Newer state (ordinal 2, e.g. "shipped") is delivered first.
    e2 = add_event(source_id, "o2", "order:1", 2)
    _process_one(client)
    assert event_status(e2) == EventStatus.delivered

    # Then a stale older update (ordinal 1, e.g. "confirmed") arrives late.
    e1 = add_event(source_id, "o1", "order:1", 1)
    _process_one(client)

    # It is dropped, not delivered — the downstream never sees the regression.
    assert event_status(e1) == EventStatus.superseded
    assert event_status(e2) == EventStatus.delivered


def test_in_order_updates_all_deliver():
    """Guard must NOT drop legitimate in-order updates."""
    source_id = add_source("ord", url="http://ord.test/hook")
    client = mock_client(200)

    e1 = add_event(source_id, "n1", "order:9", 1)
    _process_one(client)
    e2 = add_event(source_id, "n2", "order:9", 2)
    _process_one(client)

    assert event_status(e1) == EventStatus.delivered
    assert event_status(e2) == EventStatus.delivered
