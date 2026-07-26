"""Database engine and session factory (SQLAlchemy 2.0, sync).

We use the *sync* engine on purpose: the whole design leans on Postgres row
locking (`SELECT ... FOR UPDATE SKIP LOCKED`) and short explicit transactions,
which are far easier to reason about synchronously. FastAPI runs sync endpoint
functions in a threadpool, so this does not block the event loop.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

# pool_pre_ping avoids handing out a dead connection after Postgres restarts /
# idle timeouts — important for a long-lived worker process.
engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
