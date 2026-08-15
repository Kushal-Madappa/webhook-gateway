"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import Depends, FastAPI, Request, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session
from app.logging_config import configure_logging
from app.routers import events, sources, webhooks

configure_logging()
log = logging.getLogger("api")

app = FastAPI(title="Reliable Webhook Gateway", version="0.1.0")

app.include_router(sources.router)
app.include_router(webhooks.router)
app.include_router(events.router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Emit one structured line per request and attach a correlation id."""
    request_id = str(uuid.uuid4())
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    log.info(
        "request",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - start) * 1000, 2),
        },
    )
    return response


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
