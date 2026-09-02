"""
App entry — builds the FastAPI app and registers every router.

    uvicorn app.main:app --reload        (run from backend/)

    request ──> FastAPI app ──> router (api/…) ──> service (services/…) ──> DB

This file stays THIN on purpose: it wires things together and owns nothing else.
Business logic lives in ``services/``, HTTP shapes live in ``api/``. When this
file grows past ~100 lines, something is in the wrong place.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import (
    accounts,
    analytics,
    auth,
    catalogue,
    customers,
    dashboard,
    health,
    orders,
    payments,
    seller_auth,
    storefront,
    whatsapp_cloud,
)
from app.api import (
    settings as settings_routes,
)
from app.config import settings
from app.dependencies import LoginRequired, login_redirect
from app.services.media import MEDIA_ROOT, MEDIA_URL_PREFIX
from app.templating import templates

STATIC_DIR = Path(__file__).resolve().parent / "static"

logger = logging.getLogger("biashara.app")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """
    Run the job worker inside the web process, unless a separate one drains it.

    WHY A THREAD AND NOT A SECOND SERVICE. Forwarded photos are parsed off the
    request path (Meta redelivers anything slow), which needs *something* to
    drain the queue. A daemon thread here keeps the whole app one deploy and one
    bill; the queue lives in Postgres, so nothing is lost on a restart — the
    thread just reclaims and carries on. Point ``worker_in_process`` at False
    and run ``python -m app.worker`` when the web process must stay lean under
    load; ``FOR UPDATE SKIP LOCKED`` means both draining at once is safe anyway.
    """
    worker = None
    thread = None
    if settings.worker_in_process:
        # Imported here, not at module scope: importing the handlers registers
        # them, and doing it lazily keeps a plain ``import app.main`` from
        # dragging in the whole service layer.
        from app import jobs_handlers  # noqa: F401  (registers handlers on import)
        from app.worker import Worker

        worker = Worker()
        thread = threading.Thread(target=worker.run, name="intake-worker", daemon=True)
        thread.start()
        logger.info("In-process job worker started")

    try:
        yield
    finally:
        if worker is not None:
            worker.running = False  # finishes the job in hand, then the daemon exits


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    # The interactive docs page is a gift in dev and a liability in prod, where
    # it advertises the whole API surface. Gate it on environment now so nobody
    # has to remember later.
    docs_url="/docs" if not settings.is_prod else None,
    redoc_url=None,  # one docs UI is enough
    lifespan=lifespan,
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


@app.exception_handler(StarletteHTTPException)
def _not_found(request: Request, exc: Exception) -> Response:
    """
    Render a 404 a person can read, when a person is what asked.

    A SHOP LINK IS THIS PRODUCT'S WHOLE DISTRIBUTION. It gets pasted into chats,
    forwarded and put in bios, and it outlives the shop it points at — sellers
    close shops and unpublish items. So a buyer meeting a 404 is normal traffic,
    and until now they met ``{"detail":"Shop not found"}``, which in a phone's
    in-app browser is a blank screen or a downloaded file. A tapped link that
    appears to do nothing is indistinguishable from a broken app.

    ONLY FOR BROWSERS, AND ONLY FOR 404. Anything that did not ask for HTML —
    Daraja's callback, the WhatsApp webhook, the health check, a fetch — keeps the JSON body
    it expects, because a payment callback parsing an HTML error page is a worse
    failure than the one it was reporting. Every other status is left alone too:
    a 403 on the webhook must stay machine-readable.
    """
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    detail = exc.detail if isinstance(exc, StarletteHTTPException) else "Error"

    wants_html = "text/html" in request.headers.get("accept", "")
    if status == 404 and wants_html:
        return templates.TemplateResponse(request, "storefront/gone.html", {}, status_code=404)

    return JSONResponse({"detail": detail}, status_code=status)


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(seller_auth.router)
app.include_router(dashboard.router)
app.include_router(customers.router)
app.include_router(accounts.router)
app.include_router(catalogue.router)
app.include_router(analytics.router)
app.include_router(orders.router)
app.include_router(payments.router)
app.include_router(whatsapp_cloud.router)
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
