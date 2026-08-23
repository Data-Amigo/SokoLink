"""
Signing in with a WhatsApp number and a one-time code.

    GET  /seller/login    the number
    POST /seller/login    send a code
    GET  /seller/verify   the six digits
    POST /seller/verify   prove the number  ──▶ existing seller? straight in
    GET  /seller/shop     new seller: name the shop
    POST /seller/shop     create it, sign in

WHY THIS REPLACES EMAIL AND PASSWORD. The number is the thing a Kenyan seller
remembers, it is already how their customers reach them, and WhatsApp can prove
they hold it. A password is a second secret to invent, forget and reset, for an
audience whose whole business lives in one app. ``/signup`` and ``/login`` still
work for anyone already using them.

THE PHONE NEVER APPEARS IN A URL. WAYS_OF_WORKING §5 — links leak through
history, referrers and forwarding. It travels in a SIGNED cookie carrying two
states: a code was sent, versus a code was entered correctly. Signing is what
stops a browser editing "sent" into "proved", which would be the whole
authentication bypass.

THE SAME ANSWER FOR EVERY NUMBER. Asking for a code says nothing about whether
that number has a shop — otherwise this page becomes a way to enumerate which
Kenyan businesses are registered. Whether it is a sign-in or a sign-up is
decided AFTER the code is proven, not before it is sent.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.dependencies import set_session_cookie
from app.security import (
    PHONE_COOKIE,
    PHONE_PROOF_LIFETIME,
    AuthError,
    create_phone_token,
    read_phone_token,
)
from app.services.accounts import (
    SignupError,
    create_account_for_phone,
    find_by_phone,
    normalise_whatsapp,
)
from app.services.messaging import MessagingError, Messenger, get_messenger
from app.services.otp import OtpError, request_code, verify_code
from app.templating import templates

router = APIRouter(tags=["seller-auth"])


def _set_phone_cookie(response: Response, phone: str, verified: bool) -> Response:
    """
    Remember which number this browser is working with, signed.

    ``HttpOnly`` because no script needs it and its whole value is that it
    cannot be edited. ``Secure`` follows the environment rather than being
    hardcoded — a Secure cookie is silently dropped over plain http, which would
    make local development lose the number between two pages.
    """
    response.set_cookie(
        key=PHONE_COOKIE,
        value=create_phone_token(phone, verified=verified),
        max_age=int(PHONE_PROOF_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=get_settings().is_prod,
    )
    return response


def _pending_phone(request: Request, *, require_verified: bool) -> str | None:
    """
    The number this browser is working with, or None to start over.

    Args:
        request: Carries the cookie.
        require_verified: True on any step that must only be reachable after
            the code was entered correctly.

    Returns:
        The phone, or None when the cookie is missing, expired, tampered with,
        or not yet verified when verification is required.
    """
    token = request.cookies.get(PHONE_COOKIE)
    if not token:
        return None

    try:
        phone, verified = read_phone_token(token)
    except AuthError:
        return None

    if require_verified and not verified:
        return None
    return phone


@router.get("/seller/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    """Step 1: the number customers already use to reach this business."""
    return templates.TemplateResponse(
        request,
        "auth/seller_login.html",
        {"error": request.query_params.get("error"), "step": 1},
    )


@router.post("/seller/login")
def login_send_code(
    request: Request,
    phone: str = Form(...),
    db: Session = Depends(get_db),
    messenger: Messenger = Depends(get_messenger),
) -> Response:
    """
    Send a code to that number.

    Says the same thing whether or not the number has a shop. Reporting "no
    account with that number" here would turn this form into a way to test which
    Kenyan businesses are registered, one number at a time.
    """
    try:
        clean = normalise_whatsapp(phone)
    except SignupError as exc:
        return RedirectResponse(url=f"/seller/login?error={exc}", status_code=303)

    if clean is None:
        return RedirectResponse(url="/seller/login?error=Enter your number.", status_code=303)

    try:
        request_code(db, clean, messenger)
    except (OtpError, MessagingError) as exc:
        return RedirectResponse(url=f"/seller/login?error={exc}", status_code=303)

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _set_phone_cookie(
        RedirectResponse(url="/seller/verify", status_code=303), clean, verified=False
    )


@router.get("/seller/verify", response_class=HTMLResponse)
def verify_page(request: Request) -> Response:
    """Step 2: the six digits."""
    phone = _pending_phone(request, require_verified=False)
    if phone is None:
        return RedirectResponse(url="/seller/login", status_code=303)

    return templates.TemplateResponse(
        request,
        "auth/seller_verify.html",
        {"phone": phone, "error": request.query_params.get("error"), "step": 2},
    )


@router.post("/seller/verify")
def verify_submit(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """
    Check the code. A known number goes straight in; a new one names its shop.

    This is the only place that learns whether the number has a shop, and it
    only runs once the number is proven — so nothing is disclosed to anyone who
    has not just demonstrated they hold the handset.
    """
    phone = _pending_phone(request, require_verified=False)
    if phone is None:
        return RedirectResponse(url="/seller/login", status_code=303)

    try:
        ok = verify_code(db, phone, code)
    except OtpError as exc:
        db.commit()  # the attempt counter must survive a refusal
        return RedirectResponse(url=f"/seller/verify?error={exc}", status_code=303)

    if not ok:
        db.commit()  # ditto: a wrong guess costs an attempt
        return RedirectResponse(
            url="/seller/verify?error=That code is not right. Try again.", status_code=303
        )

    account = find_by_phone(db, phone)
    db.commit()

    if account is not None:
        response = RedirectResponse(url="/dashboard", status_code=303)
        set_session_cookie(response, account.id)
        response.delete_cookie(PHONE_COOKIE)
        return response

    return _set_phone_cookie(
        RedirectResponse(url="/seller/shop", status_code=303), phone, verified=True
    )


@router.get("/seller/shop", response_class=HTMLResponse)
def shop_page(request: Request) -> Response:
    """Step 3: what the shop is called. Only reachable with a proven number."""
    phone = _pending_phone(request, require_verified=True)
    if phone is None:
        return RedirectResponse(url="/seller/login", status_code=303)

    return templates.TemplateResponse(
        request,
        "auth/seller_shop.html",
        {"phone": phone, "error": request.query_params.get("error"), "step": 3},
    )


@router.post("/seller/shop")
def shop_submit(
    request: Request,
    shop_name: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """
    Create the shop and sign the seller in.

    Requires a VERIFIED phone cookie. Without that check this route would create
    an account for any number a browser cared to claim.
    """
    phone = _pending_phone(request, require_verified=True)
    if phone is None:
        return RedirectResponse(url="/seller/login", status_code=303)

    try:
        account = create_account_for_phone(db, phone=phone, shop_name=shop_name)
    except SignupError as exc:
        return RedirectResponse(url=f"/seller/shop?error={exc}", status_code=303)

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    response = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(response, account.id)
    response.delete_cookie(PHONE_COOKIE)
    return response
