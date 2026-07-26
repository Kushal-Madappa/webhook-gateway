# Reliable Webhook Gateway

Receive third-party webhooks and guarantee they are **verified, de-duplicated, ordered, retried, and never lost** before delivering them to a downstream URL — using Postgres as the only queue (no Redis, no Celery).

> Build status: work in progress — being built in six stages (skeleton → ingest → worker → reliability → ops → tests & docs). The full README, failure-modes table, architecture diagram, and design-decisions section land in the final stage.

## Stack
Python 3.11+ · FastAPI · SQLAlchemy 2.0 · Alembic · httpx · PostgreSQL · Docker Compose · pytest

## Run (Docker)
```bash
docker compose up --build
```
This starts Postgres and the API, applying Alembic migrations automatically on boot.

Check health (verifies real DB connectivity):
```bash
curl -s localhost:8000/healthz
```

## Data model
- **sources**: `id, name (unique), signing_secret, downstream_url, created_at`
- **events**: `id (uuid pk), source_id (fk), provider_event_id, resource_key, status_ordinal, payload (jsonb), status (enum), attempts, next_attempt_at, last_error, created_at, updated_at` with `UNIQUE (source_id, provider_event_id)`
