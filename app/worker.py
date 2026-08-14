"""Delivery worker — polls Postgres for due events and delivers them downstream.

Runs as its own process and is safe to run as 2+ concurrent instances.

Design (Stage 4): ONE transaction per event. Within that single transaction we
  1. lock exactly one due row with FOR UPDATE SKIP LOCKED,
  2. take a per-resource advisory lock (serialises same-resource events),
  3. run the monotonic ordering guard,
  4. deliver via httpx,
  5. write the terminal/next status,
  6. commit.
Holding the row lock across the HTTP call means a crashed worker's transaction
simply rolls back and the row returns to pending/failed automatically — there is
no "stuck delivering" state to reap. The cost is that a DB connection is held for
the duration of each delivery; at webhook scale that is fine and we scale out by
running more workers.
"""
from __future__ import annotations

import logging
import random
import signal
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Event, EventStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker] %(message)s",
)
log = logging.getLogger("worker")

# A fresh event (pending) or one whose last attempt failed (failed) can become
# due. delivering/delivered/dead/superseded are never re-claimed.
_DUE_STATUSES = (EventStatus.pending, EventStatus.failed)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def backoff_delay(attempts: int) -> timedelta:
    """Exponential backoff WITH FULL JITTER.

    Base grows as base * 2^(attempts-1), capped at max_backoff. We then pick a
    uniformly random point in [0, capped] (AWS "full jitter"). The jitter is the
    important part: without it, a downstream that went down would knock over many
    events at once, and they would all retry at the *same* backed-off instant —
    a synchronised thundering herd that re-flattens the downstream the moment it
    recovers. Randomising each retry spreads the load out.
    """
    exp = settings.base_backoff_seconds * (2 ** (attempts - 1))
    capped = min(exp, settings.max_backoff_seconds)
    return timedelta(seconds=random.uniform(0, capped))


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


def _lock_resource(session: Session, resource_key: str) -> None:
    """Take a transaction-scoped advisory lock keyed by the resource.

    This serialises ALL processing of a given resource_key across every worker,
    which is what makes the monotonic guard race-free: two workers holding
    events for the same resource cannot both pass the guard and deliver out of
    order — the second one blocks here until the first commits, then sees the
    updated max-delivered ordinal. Different resource_keys hash to different
    lock ids and still run fully in parallel.
    """
    # hashtext returns int4; cast to bigint for pg_advisory_xact_lock(bigint).
    session.execute(select(func.pg_advisory_xact_lock(cast(func.hashtext(resource_key), BigInteger))))


def _max_delivered_ordinal(session: Session, resource_key: str) -> int | None:
    return session.execute(
        select(func.max(Event.status_ordinal)).where(
            Event.resource_key == resource_key,
            Event.status == EventStatus.delivered,
        )
    ).scalar()


def process_one(session: Session, client: httpx.Client) -> str | None:
    """Process a single due event inside one transaction. Returns its id or None."""
    now = _now()

    # (1) Claim one due row. SKIP LOCKED means concurrent workers each grab a
    # different row instead of blocking on or double-taking the same one. We do
    # NOT eager-join the source here: FOR UPDATE against the nullable side of an
    # outer join is illegal in Postgres, so source is lazy-loaded below.
    event = (
        session.execute(
            select(Event)
            .where(Event.status.in_(_DUE_STATUSES), Event.next_attempt_at <= now)
            .order_by(Event.next_attempt_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .first()
    )
    if event is None:
        session.commit()  # release the (empty) snapshot
        return None

    # (2) Serialise this resource across workers before we read/write ordering.
    _lock_resource(session, event.resource_key)

    # (3) Monotonic guard. If a HIGHER status_ordinal for this resource has
    # already been delivered, this event is a stale/out-of-order update: sending
    # it would let an old state (e.g. "confirmed", ordinal 1) overwrite a newer
    # one (e.g. "shipped", ordinal 2) at the downstream. Drop it as superseded
    # rather than deliver it. Because of the advisory lock, the max we read here
    # already reflects any concurrent same-resource delivery.
    max_delivered = _max_delivered_ordinal(session, event.resource_key)
    if max_delivered is not None and event.status_ordinal < max_delivered:
        event.status = EventStatus.superseded
        event.last_error = f"superseded: ordinal {event.status_ordinal} < delivered {max_delivered}"
        event.updated_at = now
        session.commit()
        log.info(
            "superseded event=%s resource=%s ordinal=%s<%s",
            event.id, event.resource_key, event.status_ordinal, max_delivered,
        )
        return str(event.id)

    # (4) Deliver while still holding the row + advisory locks.
    ok, error = deliver(event, client)

    # (5) Record the outcome.
    if ok:
        event.status = EventStatus.delivered
        event.last_error = None
        log.info("delivered event=%s source=%s ordinal=%s",
                 event.id, event.source.name, event.status_ordinal)
    else:
        event.attempts += 1
        event.last_error = (error or "unknown error")[:2000]
        if event.attempts >= settings.max_attempts:
            # Exhausted retries -> dead-letter. Parked, not deleted, so it can be
            # inspected and replayed (Stage 5).
            event.status = EventStatus.dead
            log.warning("dead-lettered event=%s attempts=%s error=%s",
                        event.id, event.attempts, error)
        else:
            event.status = EventStatus.failed
            event.next_attempt_at = now + backoff_delay(event.attempts)
            log.warning("delivery failed event=%s attempt=%s error=%s",
                        event.id, event.attempts, error)
    event.updated_at = now
    session.commit()  # (6) releases row lock + advisory lock
    return str(event.id)


class _Stopper:
    """Flips to True on SIGTERM/SIGINT so the loop exits cleanly."""

    def __init__(self) -> None:
        self.stop = False
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, *_args) -> None:
        log.info("shutdown signal received, stopping")
        self.stop = True


def run_worker() -> None:
    log.info(
        "worker starting (poll=%ss max_attempts=%s base_backoff=%ss)",
        settings.worker_poll_interval_seconds,
        settings.max_attempts,
        settings.base_backoff_seconds,
    )
    stopper = _Stopper()
    with httpx.Client(timeout=settings.delivery_timeout_seconds) as client:
        while not stopper.stop:
            session = SessionLocal()
            try:
                handled = process_one(session, client)
            except Exception:  # noqa: BLE001 - never let the loop die on one bad event
                log.exception("worker error")
                session.rollback()
                handled = None
            finally:
                session.close()
            if handled is None:
                # Nothing due; sleep so we don't hot-spin on an empty table.
                time.sleep(settings.worker_poll_interval_seconds)
    log.info("worker stopped")


if __name__ == "__main__":
    run_worker()
