"""
The public storefront routes.

    GET /{slug}             the shop
    GET /{slug}/{id}        one product + the WhatsApp handoff

THESE ROUTES ARE REGISTERED LAST, because ``/{slug}`` would otherwise swallow
``/health``, ``/docs`` and everything else. ``RESERVED_SLUGS`` stops a seller
claiming one of those names in the first place; registration order is the
second half of that guard.

Thin on purpose: parse, delegate, render. Every rule about what a buyer may see
lives in ``services/storefront.py`` where it can be tested once.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from app.db import get_db
from app.services.media import public_url
from app.services.storefront import (
    build_whatsapp_url,
    connected_account,
    get_public_product,
    get_public_products,
    get_public_shop,
)

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# `| media` turns a stored relative path into a servable URL, and passes a
# legacy absolute URL through untouched. A filter rather than a per-view
# transform, so no template can forget it and render a raw path.
templates.env.filters["media"] = public_url

router = APIRouter(tags=["storefront"])


@router.get("/{slug}", response_class=HTMLResponse)
def shop_page(slug: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """
    A seller's public shop.

    Raises:
        HTTPException: 404 when the slug is unknown or the shop is unpublished.
            Both look identical from outside, which is correct: an unpublished
            shop is full of half-parsed drafts nobody should see.
    """
    seller = get_public_shop(db, slug)
    if seller is None:
        raise HTTPException(status_code=404, detail="Shop not found")

    return templates.TemplateResponse(
        request,
        "storefront/shop.html",
        {
            "seller": seller,
            "products": get_public_products(db, seller),
            "tiktok": connected_account(seller),
        },
    )


@router.get("/{slug}/{product_id}", response_class=HTMLResponse)
def product_page(
    slug: str, product_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """
    One product, and the WhatsApp handoff that is the point of the whole system.

    Raises:
        HTTPException: 404 when the shop or product is not publicly visible.
    """
    seller = get_public_shop(db, slug)
    if seller is None:
        raise HTTPException(status_code=404, detail="Shop not found")

    product = get_public_product(db, seller, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    return templates.TemplateResponse(
        request,
        "storefront/product.html",
        {
            "seller": seller,
            "product": product,
            "whatsapp_url": build_whatsapp_url(seller, product),
        },
    )
