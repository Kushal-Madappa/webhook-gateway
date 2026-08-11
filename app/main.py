"""FastAPI application entrypoint.

Stage 2 exposes /healthz plus the ingest and admin source endpoints. Inspection
and replay endpoints are added in a later stage.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session
from app.routers import sources, webhooks

app = FastAPI(title="Reliable Webhook Gateway", version="0.1.0")

app.include_router(sources.router)
app.include_router(webhooks.router)


@app.get("/healthz")
def healthz(response: Response, session: Session = Depends(get_session)) -> dict:
    """Liveness/readiness probe that ACTUALLY exercises the database.

    A hardcoded 200 is a lie: the process can be up while Postgres is
    unreachable, which for this service means it can accept webhooks it will
    never be able to persist. So we run a trivial round-trip query and report
    503 if it fails, letting an orchestrator pull the pod out of rotation.
    """
    try:
        session.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception as exc:  # noqa: BLE001 - health check must catch everything
        response.status_code = 503
        return {"status": "unhealthy", "database": "unreachable", "detail": str(exc)}
