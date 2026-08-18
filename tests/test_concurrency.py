"""The race that check-then-insert would lose: N identical inserts -> one row."""
import threading

from app.db import SessionLocal
from app.ingest import insert_event_idempotent
from tests.helpers import add_source, count_events

N = 12


def test_concurrent_identical_inserts_yield_one_row():
    source_id = add_source("conc")
    results: list[tuple[str, bool]] = []
    # A barrier makes all threads fire the insert at (nearly) the same instant,
    # maximising the chance of a true race on the unique index.
    barrier = threading.Barrier(N)

    def worker() -> None:
        barrier.wait()
        session = SessionLocal()
        try:
            event_id, inserted = insert_event_idempotent(
                session,
                source_id=source_id,
                provider_event_id="race",
                resource_key="r",
                status_ordinal=1,
                payload={"a": 1},
            )
            results.append((event_id, inserted))
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread created the row...
    assert sum(1 for _, inserted in results if inserted) == 1
    # ...every thread agrees on the same surviving event id...
    assert len({event_id for event_id, _ in results}) == 1
    # ...and the database holds a single row.
    assert count_events(source_id, "race") == 1
