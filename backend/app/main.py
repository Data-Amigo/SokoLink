"""
App entry — builds the FastAPI app and registers every router.

    uvicorn app.main:app --reload        (run from backend/)

    request ──> FastAPI app ──> router (api/…) ──> service (services/…) ──> DB

This file stays THIN on purpose: it wires things together and owns nothing else.
Business logic lives in ``services/``, HTTP shapes live in ``api/``. When this
file grows past ~100 lines, something is in the wrong place.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    accounts,
    analytics,
    auth,
    catalogue,
    dashboard,
    health,
    orders,
    payments,
    seller_auth,
    storefront,
)
from app.api import (
    settings as settings_routes,
)
from app.config import settings
from app.dependencies import LoginRequired, login_redirect
from app.services.media import MEDIA_ROOT, MEDIA_URL_PREFIX

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    # The interactive docs page is a gift in dev and a liability in prod, where
    # it advertises the whole API surface. Gate it on environment now so nobody
    # has to remember later.
    docs_url="/docs" if not settings.is_prod else None,
    redoc_url=None,  # one docs UI is enough
)


@app.exception_handler(LoginRequired)
def _login_required(request: Request, exc: Exception) -> RedirectResponse:
    """
    Send an anonymous visitor to sign in, then back where they were going.

    Registered here rather than caught per-route so that no page behind the
    login wall can forget to do it. A missing session is a browser concern, not
    a business rule — which is why the handler lives in the wiring file and the
    rule itself is a one-line dependency.
    """
    next_url = exc.next_url if isinstance(exc, LoginRequired) else None
    return login_redirect(next_url)


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(seller_auth.router)
app.include_router(dashboard.router)
app.include_router(accounts.router)
app.include_router(catalogue.router)
app.include_router(analytics.router)
app.include_router(orders.router)
app.include_router(payments.router)
app.include_router(settings_routes.router)

# The design system, and anything else the browser fetches by URL. Version-free
# paths for now; when caching starts to bite, this mount grows a hash.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Our own copies of covers, because the platforms' URLs expire.
#
# mkdir first: StaticFiles refuses to mount a missing directory at startup, and
# on a fresh clone nothing has been scraped yet. In production these paths move
# behind object storage and a CDN; products store a relative path, so only this
# mount changes.
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
app.mount(MEDIA_URL_PREFIX, StaticFiles(directory=MEDIA_ROOT), name="media")

# The storefront lives under /shop/{slug}, so registration order no longer
# matters here. It used to own `/{slug}` at the root, which matched everything
# and made "register this router last" a rule the whole file depended on
# silently. Namespacing it removed that rule rather than documenting it harder.
app.include_router(storefront.router)
