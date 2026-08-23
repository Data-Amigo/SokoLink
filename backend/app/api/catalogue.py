"""
The seller's catalogue, and the publish gate.

    GET  /products                      the review queue
    GET  /products/new                  add one by hand
    POST /products/new                  create it, as a DRAFT
    POST /products/{id}/publish         make one item buyable
    POST /products/{id}/unpublish       take it back off
    POST /settings/shop/open            open the storefront
    POST /settings/shop/close           close it

THIS SCREEN IS WHERE A SHOP BECOMES REAL. Until a seller presses Publish, every
product is a DRAFT and ``/shop/{slug}`` shows nothing — which is exactly what
happened before this existed: the rule "publish is a human gate" was enforced by
the database and implemented by nobody.

THE QUEUE IS ORDERED BY THE MODEL'S OWN DOUBT, lowest confidence first. That is
the point of the page rather than a detail of it: a wrong price hides in the
drafts the AI was least sure about, and a seller who publishes down a
date-ordered list will meet those last, after the habit of clicking Publish has
already formed.

WHY NOTHING HERE SITS UNDER ``/shop/…``.
``/shop/{slug}`` is the PUBLIC storefront and takes no authentication, and a
seller may legitimately hold the slug "open" or "close". Putting privileged
actions in that namespace would mean a public route and a seller-only one
sharing a prefix, resolved by registration order — which works until somebody
reorders the routers. ``/settings/shop/…`` is unambiguous by construction.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.dependencies import current_account
from app.models import Account, ProductStatus
from app.services.accounts import SignupError, normalise_whatsapp
from app.services.catalogue import (
    PublishError,
    catalogue_summary,
    create_product,
    list_products,
    publish_product,
    publish_shop,
    unpublish_product,
    unpublish_shop,
)
from app.services.orders import get_payment_method
from app.templating import templates

router = APIRouter(tags=["catalogue"])


def _back(error: str | None = None) -> RedirectResponse:
    """
    Redirect after a POST so a refresh cannot repeat it.

    303 forces the follow-up to be a GET. Publishing twice is harmless, but a
    seller who refreshes after closing their shop must not be asked to close it
    again — and the pattern should not depend on which action it wraps.
    """
    path = "/products"
    if error:
        path = f"{path}?error={error}"
    return RedirectResponse(url=path, status_code=303)


@router.get("/products", response_class=HTMLResponse)
def products_page(
    request: Request,
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    Everything in the seller's catalogue, worst parse first.

    Also carries the two facts that decide whether the shop can sell at all:
    whether it is open, and whether a payment method exists. Both are shown
    here rather than left to be discovered, because a seller who publishes ten
    products into a closed shop has done ten pieces of work for nothing.
    """
    seller = account.seller

    return templates.TemplateResponse(
        request,
        "app/products.html",
        {
            "account": account,
            "seller": seller,
            "products": list_products(db, seller) if seller is not None else [],
            "summary": (
                catalogue_summary(db, seller)
                if seller is not None
                else {"published": 0, "draft": 0, "needs_price": 0}
            ),
            "payment_method": get_payment_method(db, seller) if seller is not None else None,
            "PUBLISHED": ProductStatus.PUBLISHED.value,
            "nav": "products",
            "error": request.query_params.get("error"),
        },
    )


@router.get("/products/new", response_class=HTMLResponse)
def product_new_form(
    request: Request,
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    The form for typing a product in by hand.

    Exists because a seller has to be able to sell something before any
    ingestion path is built. It is also the fallback that never breaks: no
    scrape, no AI, no WhatsApp — just a name and a price.
    """
    return templates.TemplateResponse(
        request,
        "app/product_new.html",
        {
            "account": account,
            "seller": account.seller,
            "nav": "products",
            "error": request.query_params.get("error"),
        },
    )


@router.post("/products/new")
def product_new(
    title: str = Form(...),
    price_kes: str = Form(""),
    stock: str = Form("1"),
    description: str = Form(""),
    category: str = Form(""),
    sizes: str = Form(""),
    unit_quantity: str = Form(""),
    unit_label: str = Form(""),
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> Response:
    """
    Create the product, as a DRAFT.

    Numbers arrive as strings because an empty text box submits "" rather than
    nothing, and int() would raise on it. Parsing here — where the seller can be
    told "that is not a number" — is better than a 422 they cannot read.
    """
    seller = account.seller
    if seller is None:
        return _back("Your shop is not set up yet.")

    def number(raw: str, field: str) -> int | None:
        text = raw.strip()
        if not text:
            return None
        try:
            return int(text.replace(",", "").replace(" ", ""))
        except ValueError:
            raise PublishError(f"{field} must be a number.") from None

    try:
        price = number(price_kes, "Price")
        count = number(stock, "Stock")
        pack = number(unit_quantity, "Pack size")
        product = create_product(
            db,
            seller,
            title=title,
            price_kes=price,
            stock=1 if count is None else count,
            description=description,
            category=category,
            sizes=sizes,
            unit_quantity=pack,
            unit_label=unit_label,
        )
    except PublishError as exc:
        return RedirectResponse(url=f"/products/new?error={exc}", status_code=303)

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return RedirectResponse(url=f"/products?added={product.id}", status_code=303)


@router.post("/products/{product_id}/publish")
def product_publish(
    product_id: int,
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> Response:
    """
    Put one product on the storefront.

    Refused without a price — the rail that stops the AI's guess becoming a
    number someone pays. The message names the item, because a seller with
    thirty drafts needs to know which one.
    """
    seller = account.seller
    if seller is None:
        return _back("Your shop is not set up yet.")

    try:
        publish_product(db, seller, product_id)
    except PublishError as exc:
        return _back(str(exc))

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back()


@router.post("/products/{product_id}/unpublish")
def product_unpublish(
    product_id: int,
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> Response:
    """Take one product off the storefront. Orders already placed are untouched."""
    seller = account.seller
    if seller is None:
        return _back("Your shop is not set up yet.")

    try:
        unpublish_product(db, seller, product_id)
    except PublishError as exc:
        return _back(str(exc))

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back()


@router.post("/settings/shop/open")
def shop_open(
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> Response:
    """
    Open the storefront to buyers.

    Refused without a WhatsApp number: a live shop nobody can contact is a dead
    end for every buyer who reaches it.
    """
    seller = account.seller
    if seller is None:
        return _back("Your shop is not set up yet.")

    try:
        publish_shop(db, seller)
    except PublishError as exc:
        return _back(str(exc))

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back()


@router.post("/settings/shop/close")
def shop_close(
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> Response:
    """Close the storefront. The slug is kept — it has already been shared."""
    seller = account.seller
    if seller is not None:
        unpublish_shop(db, seller)
    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back()


@router.post("/settings/shop/contact")
def shop_contact(
    whatsapp_number: str = Form(...),
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> Response:
    """
    Set the number buyers reach this seller on.

    EXISTS BECAUSE SELLERS COULD GET STUCK. Signup collects a number now, but
    accounts created before it did have none — and ``published_needs_whatsapp``
    refuses to open their shop. Without this form they were blocked with no way
    forward, which is the worst possible place for a dead end: after the work of
    adding stock.

    Normalised to 2547XXXXXXXX on the way in, so every ``wa.me`` link built from
    it resolves.
    """
    seller = account.seller
    if seller is None:
        return RedirectResponse(url="/settings/payment?error=No shop yet.", status_code=303)

    try:
        seller.whatsapp_number = normalise_whatsapp(whatsapp_number)
    except SignupError as exc:
        return RedirectResponse(url=f"/settings/payment?error={exc}", status_code=303)

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return RedirectResponse(url="/settings/payment?saved=1", status_code=303)
