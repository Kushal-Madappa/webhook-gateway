"""Runtime configuration, read from environment variables.

We deliberately avoid pydantic-settings / extra config libraries — the stack is
frozen (see README design decisions) and a handful of env vars needs nothing
more than os.getenv. All knobs the worker and API share live here so there is a
single source of truth.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _build_database_url() -> str:
    # A full DATABASE_URL always wins (this is what docker-compose injects and
    # what CI / prod would set). Otherwise assemble one from discrete POSTGRES_*
    # parts so local `psql`-style env also works.
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    user = os.getenv("POSTGRES_USER", "gateway")
    password = os.getenv("POSTGRES_PASSWORD", "gateway")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "gateway")
    # psycopg (v3) sync driver.
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


@dataclass(frozen=True)
class Settings:
    database_url: str = _build_database_url()

    # Admin bearer token protecting POST /v1/sources. Overridable per env.
    admin_token: str = os.getenv("ADMIN_TOKEN", "change-me-admin-token")

    # Worker tuning (used from Stage 3 onward; defined here so config stays central).
    max_attempts: int = int(os.getenv("MAX_ATTEMPTS", "6"))
    base_backoff_seconds: float = float(os.getenv("BASE_BACKOFF_SECONDS", "2"))
    max_backoff_seconds: float = float(os.getenv("MAX_BACKOFF_SECONDS", "3600"))
    worker_poll_interval_seconds: float = float(os.getenv("WORKER_POLL_INTERVAL_SECONDS", "1"))
    worker_batch_size: int = int(os.getenv("WORKER_BATCH_SIZE", "10"))
    delivery_timeout_seconds: float = float(os.getenv("DELIVERY_TIMEOUT_SECONDS", "10"))


settings = Settings()
