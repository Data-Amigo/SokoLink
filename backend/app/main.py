"""
App entry — builds the FastAPI app and registers every router.

    uvicorn app.main:app --reload        (run from backend/)

    request ──> FastAPI app ──> router (api/…) ──> service (services/…) ──> DB

This file stays THIN on purpose: it wires things together and owns nothing else.
Business logic lives in ``services/``, HTTP shapes live in ``api/``. When this
file grows past ~100 lines, something is in the wrong place.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api import health
from app.config import settings

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    # The interactive docs page is a gift in dev and a liability in prod, where
    # it advertises the whole API surface. Gate it on environment now so nobody
    # has to remember later.
    docs_url="/docs" if not settings.is_prod else None,
    redoc_url=None,  # one docs UI is enough
)

app.include_router(health.router)
