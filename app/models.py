"""SQLAlchemy 2.0 ORM models: `sources` and `events`.

The schema here is the single source of truth that Alembic autogenerate compares
against. The Alembic migration in migrations/versions/ is hand-written to match.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid


class Base(DeclarativeBase):
    pass


class EventStatus(str, enum.Enum):
    """Lifecycle of an event.

    pending    -> waiting to be picked up by a worker
    delivering -> a worker has locked it and is attempting delivery
    delivered  -> downstream returned 2xx
    failed     -> a delivery attempt failed but attempts remain (will retry)
    dead       -> exhausted max_attempts; parked in the dead-letter queue
    superseded -> dropped by the monotonic guard: a newer status_ordinal for the
                  same resource_key was already delivered, so this stale update
                  must never reach the downstream
    """

    pending = "pending"
    delivering = "delivering"
    delivered = "delivered"
    failed = "failed"
    dead = "dead"
    superseded = "superseded"


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # name is the URL path segment used at ingest (/v1/webhooks/{source}); unique.
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # Shared secret used to verify the HMAC signature on incoming webhooks.
    signing_secret: Mapped[str] = mapped_column(Text, nullable=False)
    # Where verified events get delivered.
    downstream_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    events: Mapped[list["Event"]] = relationship(back_populates="source")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        # THE idempotency guarantee. Two webhooks with the same
        # (source, provider_event_id) can only ever produce one row — enforced by
        # the database, not by application-level check-then-insert (which races).
        UniqueConstraint("source_id", "provider_event_id", name="uq_events_source_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    # The provider's own id for this event (from payload/header). Together with
    # source_id this is the dedupe key.
    provider_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Identifies the logical resource an event is about (e.g. "order:123"). The
    # monotonic ordering guard operates per resource_key.
    resource_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Monotonic version/sequence for the resource. Higher wins; a lower ordinal
    # arriving after a higher one has been delivered is stale and dropped.
    status_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[EventStatus] = mapped_column(
        # native_enum -> a real Postgres ENUM type named event_status.
        Enum(EventStatus, name="event_status", native_enum=True),
        nullable=False,
        default=EventStatus.pending,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    source: Mapped["Source"] = relationship(back_populates="events")
