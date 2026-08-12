"""Delivery worker — polls Postgres for due work and delivers them downstream.

Runs as its own process, and is safe to run as 2+ concurrent instances (that is
the entire reason for SELECT ... FOR UPDATE SKIP LOCKED below).

Stage 3 scope: claim due work, deliver via httpx, transition delivered/failed.
Stage 4 adds exponential backoff + jitter, the dead-letter transition at
max_attempts, and the monotonic ordering guard.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.db import SessionLocal
from app.models import Event, EventStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker] %(message)s",
)
log = logging.getLogger("worker")

# Statuses that represent "retryable, may become due": a fresh event (pending)
# or one whose last attempt failed (failed). delivering/delivered/dead are not
# re-claimed here.
_DUE_STATUSES = (EventStatus.pending, EventStatus.failed)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def claim_due_events(session: Session, batch_size: int) -> list[Event]:
    """Atomically claim up to `batch_size` due events for THIS worker.

    The subquery selects due rows and takes a row lock with SKIP LOCKED: any row
    another worker has already locked is skipped rather than waited on, so N
    workers partition the backlog into disjoint sets instead of colliding or
    serializing behind each other. We then flip the claimed rows to `delivering`
    and commit, which both records the claim and releases the locks — after the
    commit the rows no longer match the due filter, so no other worker will pick
    them up. Without SKIP LOCKED, a second worker would block on the first's
    locked rows (throughput collapses) or, with a naive non-locking SELECT, two
    workers would read the same pending row and deliver it twice.
    """
    now = _now()
    locked_ids = (
        session.execute(
            select(Event.id)
            .where(Event.status.in_(_DUE_STATUSES), Event.next_attempt_at <= now)
            .order_by(Event.next_attempt_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    if not locked_ids:
        session.commit()  # end the (empty) transaction and release the snapshot
        return []

    session.execute(
        update(Event)
        .where(Event.id.in_(locked_ids))
        .values(status=EventStatus.delivering, updated_at=now)
    )
    # Load the claimed rows (with their Source) to deliver. expire_on_commit is
    # False on the session, so these stay usable after the commit below.
    events = (
        session.execute(
            select(Event).options(joinedload(Event.source)).where(Event.id.in_(locked_ids))
        )
        .scalars()
        .all()
    )
    session.commit()
    return events


def deliver(event: Event, client: httpx.Client) -> tuple[bool, str | None]:
    """POST the payload to the source's downstream_url. Returns (ok, error)."""
    url = event.source.downstream_url
    try:
        resp = client.post(url, json=event.payload)
    except httpx.HTTPError as exc:
        return False, f"transport error: {exc!r}"
    if 200 <= resp.status_code < 300:
        return True, None
    return False, f"downstream returned HTTP {resp.status_code}"


def mark_delivered(session: Session, event_id) -> None:
    session.execute(
        update(Event)
        .where(Event.id == event_id)
        .values(status=EventStatus.delivered, last_error=None, updated_at=_now())
    )
    session.commit()


def mark_failed(session: Session, event_id, error: str) -> None:
    """Stage 3: bump attempts, go back to `failed`, retry after a fixed delay.

    Exponential backoff + jitter and the dead-letter transition replace this
    fixed delay in Stage 4.
    """
    now = _now()
    retry_at = now + timedelta(seconds=settings.base_backoff_seconds)
    session.execute(
        update(Event)
        .where(Event.id == event_id)
        .values(
            status=EventStatus.failed,
            attempts=Event.attempts + 1,  # atomic increment in SQL
            next_attempt_at=retry_at,
            last_error=error[:2000],
            updated_at=now,
        )
    )
    session.commit()


def process_once(client: httpx.Client) -> int:
    """Claim and process one batch. Returns how many events were handled."""
    session = SessionLocal()
    try:
        events = claim_due_events(session, settings.worker_batch_size)
    finally:
        session.close()

    for event in events:
        # Deliver OUTSIDE any DB transaction so a slow downstream never holds a
        # lock. Each finalize runs in its own short transaction.
        ok, error = deliver(event, client)
        session = SessionLocal()
        try:
            if ok:
                mark_delivered(session, event.id)
                log.info("delivered event=%s source=%s", event.id, event.source.name)
            else:
                mark_failed(session, event.id, error or "unknown error")
                log.warning("delivery failed event=%s error=%s", event.id, error)
        finally:
            session.close()
    return len(events)


class _Stopper:
    """Flips to True on SIGTERM/SIGINT so the loop drains and exits cleanly."""

    def __init__(self) -> None:
        self.stop = False
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, *_args) -> None:
        log.info("shutdown signal received, stopping after current batch")
        self.stop = True


def run_worker() -> None:
    log.info(
        "worker starting (batch=%s poll=%ss)",
        settings.worker_batch_size,
        settings.worker_poll_interval_seconds,
    )
    stopper = _Stopper()
    with httpx.Client(timeout=settings.delivery_timeout_seconds) as client:
        while not stopper.stop:
            try:
                handled = process_once(client)
            except Exception:  # noqa: BLE001 - never let the loop die on one bad batch
                log.exception("worker batch error")
                handled = 0
            if handled == 0:
                # Nothing due; sleep so we don't hot-spin on an empty table.
                time.sleep(settings.worker_poll_interval_seconds)
    log.info("worker stopped")


if __name__ == "__main__":
    run_worker()
