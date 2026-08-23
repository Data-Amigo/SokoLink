"""
The public storefront routes.

    GET  /shop/{slug}                        the shop
    GET  /shop/{slug}/cart                   the basket
    POST /shop/{slug}/cart/add               add an item
    POST /shop/{slug}/cart/update            change a quantity
    POST /shop/{slug}/cart/remove            drop a line
    GET  /shop/{slug}/checkout               who the buyer is
    POST /shop/{slug}/checkout               freeze the basket into an order
    GET  /shop/{slug}/order/{ref}            pay, then the receipt
    POST /shop/{slug}/order/{ref}/claim      "I have paid", unverified
    GET  /shop/{slug}/{id}                   one product + the WhatsApp handoff

WHY THE ``/shop`` PREFIX. These routes used to live at the root, owning
``/{slug}`` — which matches *everything*. That cost two separate guards: the
router had to be registered last in ``main.py`` (so it did not swallow
``/health`` and ``/docs``), and ``RESERVED_SLUGS`` had to name every current and
future top-level path so a seller could not claim one. Both were load-bearing,
neither was visible at the point of use, and adding a route without updating the
reserved list would silently make a seller's shop unreachable.

Namespacing under ``/shop`` deletes both problems: a slug can no longer collide
with anything, registration order stops mattering, and ``/cart``, ``/checkout``
and ``/orders`` are free for the taking. ``RESERVED_SLUGS`` shrinks to the one
job it still has — stopping impersonation of the platform itself.

ROUTE ORDER STILL MATTERS ONCE, LOCALLY: ``/cart`` is declared before
``/{product_id}``, or FastAPI would try to parse "cart" as an integer id.

THE CART COOKIE IS SET BY THESE ROUTES, not by the service. A service has no
response object, and giving it one would drag HTTP into the layer that must not
know about it. Every route that may create a basket therefore ends by calling
``_with_cart_cookie``.

Thin on purpose: parse, delegate, render. Every rule about what a buyer may see
lives in ``services/storefront.py``, and every rule about what a basket may hold
lives in ``services/cart.py``, where they can be tested once.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from app.config import get_settings
from app.db import get_db
from app.dependencies import optional_account
from app.models import Account, Cart, Seller
from app.services.cart import (
    CART_COOKIE,
    CartError,
    add_item,
    get_cart,
    get_or_create_cart,
    remove_item,
    set_quantity,
    unavailable_lines,
)
from app.services.orders import (
    OrderError,
    claim_payment,
    get_order,
    get_payment_method,
    place_order,
)
from app.services.storefront import (
    DEFAULT_SORT,
    SORT_OPTIONS,
    build_shop_whatsapp_url,
    build_whatsapp_url,
    connected_account,
    get_categories,
    get_own_shop,
    get_public_product,
    get_public_products,
    get_public_shop,
)
from app.templating import templates

router = APIRouter(prefix="/shop", tags=["storefront"])

#: A basket outlives a shopping trip but not a season. Long enough that a buyer
#: who closes WhatsApp and comes back tomorrow still has their items; short
#: enough that a shared phone does not surface a stranger's basket months later.
CART_COOKIE_MAX_AGE = 60 * 60 * 24 * 30


def _load_shop(db: Session, slug: str) -> Seller:
    """
    The shop a buyer may see, or a 404.

    Raises:
        HTTPException: 404 when the slug is unknown or the shop is unpublished.
            Both look identical from outside, which is correct: an unpublished
            shop is full of half-parsed drafts nobody should see.
    """
    seller = get_public_shop(db, slug)
    if seller is None:
        raise HTTPException(status_code=404, detail="Shop not found")
    return seller


def _load_shop_or_preview(db: Session, slug: str, account: Account | None) -> tuple[Seller, bool]:
    """
    The shop, plus whether this is the owner previewing a closed one.

    Args:
        db: Session.
        slug: The shop.
        account: Whoever is signed in, or None.

    Returns:
        ``(seller, is_preview)``. ``is_preview`` is True only when the shop is
        closed and the viewer owns it.

    Raises:
        HTTPException: 404 when the shop is closed and the viewer is anyone else
            — identical to an unknown slug, as before.

    Notes:
        ONLY THE READ PAGES USE THIS. Cart and checkout still go through
        :func:`_load_shop`, so a closed shop cannot take an order even from its
        own owner. Previewing answers "what will a buyer see"; it does not turn
        the shop on, and the publish gate is unchanged.
    """
    seller = get_public_shop(db, slug)
    if seller is not None:
        return seller, False

    own = get_own_shop(db, slug, account)
    if own is not None:
        return own, True

    raise HTTPException(status_code=404, detail="Shop not found")


def _with_cart_cookie(response: Response, cart: Cart) -> Response:
    """
    Attach the basket's token to the response.

    ``HttpOnly`` because no script needs it and a stolen token is a stolen
    basket, complete with whatever delivery details W2 will add. ``Secure``
    follows the environment rather than being hardcoded on: a Secure cookie is
    silently dropped over plain http, which would make local development add to
    a basket that never persists.
    """
    response.set_cookie(
        key=CART_COOKIE,
        value=cart.token,
        max_age=CART_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=get_settings().is_prod,
    )
    return response


def _back_to(slug: str, path: str = "") -> RedirectResponse:
    """
    Redirect after a POST, so a refresh does not repeat it.

    303 rather than 302: it forces the follow-up to be a GET, which is the whole
    point of the pattern. A buyer who taps "+" twice on a flaky connection must
    not end up adding two.
    """
    return RedirectResponse(url=f"/shop/{slug}{path}", status_code=303)


@router.get("/{slug}", response_class=HTMLResponse)
def shop_page(
    slug: str,
    request: Request,
    category: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    db: Session = Depends(get_db),
    account: Account | None = Depends(optional_account),
) -> Response:
    """
    A seller's public shop, optionally filtered by category, search or sort.

    Args:
        slug: The shop.
        request: For the cart cookie and template context.
        category: One category pill, or None for all.
        q: Free-text search.
        sort: One of ``SORT_OPTIONS``; anything else falls back to newest.
        db: Session.
        account: Whoever is signed in — so a seller can preview a closed shop.

    Notes:
        THE OWNER SEES THIS PAGE EVEN WHEN THE SHOP IS CLOSED, marked as a
        preview. "View store" is the only honest answer to "what will a buyer
        get?", and it is most needed before opening — which is exactly when the
        public route 404s.
    """
    seller, preview = _load_shop_or_preview(db, slug, account)

    # An unknown sort is corrected rather than rejected. A buyer who edited the
    # URL, or a stale link, should get a sensible page and not an error.
    if sort not in SORT_OPTIONS:
        sort = DEFAULT_SORT

    response = templates.TemplateResponse(
        request,
        "storefront/shop.html",
        {
            "seller": seller,
            "products": get_public_products(db, seller, category=category, search=q, sort=sort),
            "categories": get_categories(db, seller),
            "category": category,
            "search": q,
            "sort": sort,
            "sort_options": SORT_OPTIONS,
            "tiktok": connected_account(seller),
            "whatsapp_url": build_shop_whatsapp_url(seller),
            "cart": get_cart(db, request.cookies.get(CART_COOKIE), seller),
            "preview": preview,
        },
    )
    return response


@router.get("/{slug}/cart", response_class=HTMLResponse)
def cart_page(slug: str, request: Request, db: Session = Depends(get_db)) -> Response:
    """The basket, with anything no longer purchasable called out."""
    seller = _load_shop(db, slug)
    cart = get_cart(db, request.cookies.get(CART_COOKIE), seller)

    return templates.TemplateResponse(
        request,
        "storefront/cart.html",
        {
            "seller": seller,
            "cart": cart,
            "unavailable": unavailable_lines(cart) if cart else [],
            "error": request.query_params.get("error"),
        },
    )


@router.post("/{slug}/cart/add")
def cart_add(
    slug: str,
    request: Request,
    product_id: int = Form(...),
    selected_variant: str = Form(""),
    quantity: int = Form(1),
    db: Session = Depends(get_db),
) -> Response:
    """
    Add an item to the basket, creating one if this is the buyer's first tap.

    A refused add sends the buyer back to the product page with the reason
    rather than to a generic error: the fix is nearly always to pick a different
    size or a smaller quantity, and that choice lives on that page.
    """
    seller = _load_shop(db, slug)
    cart = get_or_create_cart(db, request.cookies.get(CART_COOKIE), seller)

    try:
        add_item(db, cart, product_id, quantity=quantity, selected_variant=selected_variant)
    except CartError as exc:
        return _with_cart_cookie(_back_to(slug, f"/{product_id}?error={exc}"), cart)

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _with_cart_cookie(_back_to(slug, "/cart"), cart)


@router.post("/{slug}/cart/update")
def cart_update(
    slug: str,
    request: Request,
    item_id: int = Form(...),
    quantity: int = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Change one line's quantity. Zero removes it."""
    seller = _load_shop(db, slug)
    cart = get_cart(db, request.cookies.get(CART_COOKIE), seller)
    if cart is None:
        return _back_to(slug, "/cart")

    try:
        set_quantity(db, cart, item_id, quantity)
    except CartError as exc:
        return _back_to(slug, f"/cart?error={exc}")

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back_to(slug, "/cart")


@router.post("/{slug}/cart/remove")
def cart_remove(
    slug: str,
    request: Request,
    item_id: int = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Drop one line from the basket."""
    seller = _load_shop(db, slug)
    cart = get_cart(db, request.cookies.get(CART_COOKIE), seller)
    if cart is None:
        return _back_to(slug, "/cart")

    try:
        remove_item(db, cart, item_id)
    except CartError as exc:
        return _back_to(slug, f"/cart?error={exc}")

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back_to(slug, "/cart")


@router.get("/{slug}/checkout", response_class=HTMLResponse)
def checkout_page(slug: str, request: Request, db: Session = Depends(get_db)) -> Response:
    """
    Where the buyer identifies themselves.

    The webview knows nothing about them — a link opened from a status or a
    channel is just a browser tab, with no Meta user id and no phone number. So
    everything is asked for, once: the name, the M-Pesa line, where it goes, and
    explicit consent for a WhatsApp receipt.
    """
    seller = _load_shop(db, slug)
    cart = get_cart(db, request.cookies.get(CART_COOKIE), seller)

    if cart is None or not cart.items:
        return _back_to(slug, "/cart")

    return templates.TemplateResponse(
        request,
        "storefront/checkout.html",
        {
            "seller": seller,
            "cart": cart,
            "method": get_payment_method(db, seller),
            "unavailable": unavailable_lines(cart),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/{slug}/checkout")
def checkout_submit(
    slug: str,
    request: Request,
    buyer_name: str = Form(...),
    buyer_phone: str = Form(...),
    delivery_address: str = Form(""),
    delivery_note: str = Form(""),
    whatsapp_opt_in: bool = Form(False),
    db: Session = Depends(get_db),
) -> Response:
    """
    Freeze the basket into an order and send the buyer to the payment page.

    ``whatsapp_opt_in`` arrives only when the checkbox was ticked — an unchecked
    box submits nothing at all, which is exactly the semantics consent needs.
    """
    seller = _load_shop(db, slug)
    cart = get_cart(db, request.cookies.get(CART_COOKIE), seller)

    if cart is None or not cart.items:
        return _back_to(slug, "/cart")

    try:
        order = place_order(
            db,
            cart,
            buyer_name=buyer_name,
            buyer_phone=buyer_phone,
            delivery_address=delivery_address,
            delivery_note=delivery_note,
            whatsapp_opt_in=whatsapp_opt_in,
        )
    except OrderError as exc:
        return _back_to(slug, f"/checkout?error={exc}")

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back_to(slug, f"/order/{order.reference}")


@router.get("/{slug}/order/{reference}", response_class=HTMLResponse)
def order_page(
    slug: str, reference: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    """
    Payment instructions, then the receipt — one page, driven by status.

    ONE PAGE ON PURPOSE. The buyer lands here from checkout, leaves to pay in
    M-Pesa, and comes back. If that return landed somewhere different from where
    they left, they would not believe it was the same order. The status decides
    what the page says; the URL never changes.

    THE REFERENCE IS THE ONLY CREDENTIAL, which is why it is unguessable: there
    is no buyer account, so holding the link is what proves the order is yours.
    It is still scoped to the shop, so one seller's URL cannot surface another's.

    Raises:
        HTTPException: 404 when the reference is unknown or belongs elsewhere.
    """
    seller = _load_shop(db, slug)

    order = get_order(db, reference)
    if order is None or order.seller_id != seller.id:
        raise HTTPException(status_code=404, detail="Order not found")

    return templates.TemplateResponse(
        request,
        "storefront/order.html",
        {
            "seller": seller,
            "order": order,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/{slug}/order/{reference}/claim")
def order_claim(
    slug: str,
    reference: str,
    mpesa_code: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """
    Record the buyer's assertion that they have paid.

    **This does not mark the order paid.** The code is text a buyer typed and
    looks identical whether it came off a real M-Pesa message or was invented.
    The order moves to ``awaiting_confirmation``, and the seller — looking at
    their own phone — is the only one who can settle it.
    """
    seller = _load_shop(db, slug)

    order = get_order(db, reference)
    if order is None or order.seller_id != seller.id:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        claim_payment(db, order, mpesa_code)
    except OrderError as exc:
        return _back_to(slug, f"/order/{reference}?error={exc}")

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back_to(slug, f"/order/{reference}")


@router.get("/{slug}/{product_id}", response_class=HTMLResponse)
def product_page(
    slug: str,
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
    account: Account | None = Depends(optional_account),
) -> Response:
    """
    One product, and the two things a buyer can do with it.

    Raises:
        HTTPException: 404 when the shop or product is not publicly visible —
            unless the viewer owns the shop, who may preview it closed.
    """
    seller, preview = _load_shop_or_preview(db, slug, account)

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
            "cart": get_cart(db, request.cookies.get(CART_COOKIE), seller),
            "error": request.query_params.get("error"),
            "preview": preview,
        },
    )
