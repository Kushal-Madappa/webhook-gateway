"""Test fixtures.

We point the app at a dedicated `gateway_test` database (created on demand) and
build the schema from the ORM metadata. Env is set BEFORE any app import so
`app.config.settings` reads these values. The worker knobs are tuned for speed:
zero backoff makes a failed event immediately due again, and max_attempts=3 keeps
the retry->dead test short.
"""
import os

_DEFAULT_TEST_URL = "postgresql+psycopg://gateway:gateway@localhost:5433/gateway_test"
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", _DEFAULT_TEST_URL)
# Force deterministic test values, overriding anything the container env sets.
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["MAX_ATTEMPTS"] = "3"
os.environ["BASE_BACKOFF_SECONDS"] = "0"
os.environ["MAX_BACKOFF_SECONDS"] = "0"

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.models import Base  # noqa: E402


def _ensure_database(url: str) -> None:
    """CREATE DATABASE <test db> if it does not exist yet."""
    parsed = make_url(url)
    admin = parsed.set(database="postgres")
    engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": parsed.database}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _setup_schema():
    _ensure_database(os.environ["DATABASE_URL"])
    from app.db import engine

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def _clean_tables(_setup_schema):
    """Each test starts from empty tables."""
    from app.db import engine

    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE events, sources RESTART IDENTITY CASCADE"))


@pytest.fixture()
def db_session():
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
