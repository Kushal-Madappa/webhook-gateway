"""Admin endpoint to register sources. Protected by a bearer token."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Source
from app.schemas import SourceCreate, SourceOut
from app.security import verify_admin_token

router = APIRouter(prefix="/v1", tags=["sources"])


@router.post("/sources", response_model=SourceOut, status_code=201)
def create_source(
    body: SourceCreate,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Source:
    if not verify_admin_token(authorization):
        raise HTTPException(status_code=401, detail="invalid or missing admin token")

    source = Source(
        name=body.name,
        signing_secret=body.signing_secret,
        downstream_url=body.downstream_url,
    )
    session.add(source)
    try:
        session.commit()
    except IntegrityError:
        # Unique name violation -> the DB is the arbiter here too (no race).
        session.rollback()
        raise HTTPException(status_code=409, detail="source name already exists")
    session.refresh(source)
    return source
