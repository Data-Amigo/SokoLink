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
from fastapi.staticfiles import StaticFiles

from app.api import health, storefront
from app.config import settings
from app.services.media import MEDIA_ROOT, MEDIA_URL_PREFIX

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

# Our own copies of covers, because the platforms' URLs expire.
#
# mkdir first: StaticFiles refuses to mount a missing directory at startup, and
# on a fresh clone nothing has been scraped yet. In production these paths move
# behind object storage and a CDN; products store a relative path, so only this
# mount changes.
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
app.mount(MEDIA_URL_PREFIX, StaticFiles(directory=MEDIA_ROOT), name="media")

# REGISTERED LAST, and it must stay last. The storefront owns `/{slug}`, which
# would otherwise match /health, /docs and every future route. FastAPI matches
# in registration order, so anything added below this line is unreachable.
app.include_router(storefront.router)
