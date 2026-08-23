"""
Sign up, sign in, sign out.

    GET  /signup ──> form
    POST /signup ──> create_account ──> cookie ──> /dashboard

    GET  /login  ──> form
    POST /login  ──> authenticate ──> cookie ──> next or /dashboard

    POST /logout ──> clear cookie ──> /login

THIN ON PURPOSE. Every rule about what makes a valid account, and every
anti-enumeration decision, lives in ``services/accounts.py`` where it is tested
once. This file does four things: read a form, call the service, set a cookie,
render a template.

FORMS, NOT JSON. These are HTML pages posting to themselves. On failure they
re-render the form with the error and the typed values still in place — a
seller on a slow connection who mistypes a password must not lose their email
address too.

WHY 303 ON SUCCESS. Post/Redirect/Get. A plain 200 after a POST leaves the
browser holding a resubmittable form: refresh, and the seller is either signing
in twice or being told their email is already registered by their own signup.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.dependencies import clear_session_cookie, optional_account, set_session_cookie
from app.models import Account
from app.security import MIN_PASSWORD_LENGTH, AuthError
from app.services.accounts import SignupError, authenticate, create_account
from app.templating import templates

router = APIRouter(tags=["auth"])

#: Where a seller lands when they have not been sent anywhere in particular.
HOME = "/dashboard"


def _safe_next(next_url: str | None) -> str:
    """
    Decide where to send someone after a successful sign-in.

    Args:
        next_url: The ``?next=`` value, which arrived from the query string and
            is therefore attacker-controlled.

    Returns:
        A same-site path, or :data:`HOME`.

    Notes:
        An unchecked redirect target is a phishing primitive: a link to the
        genuine biasharamall.com login that bounces to an attacker's clone
        afterwards. Only a single leading ``/`` is accepted — ``//evil.com`` is
        a protocol-relative URL and would otherwise sail through.
    """
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return HOME


# ── Sign up ──────────────────────────────────────────────────────────────────


@router.get("/signup", response_class=HTMLResponse)
def signup_form(
    request: Request,
    account: Account | None = Depends(optional_account),
) -> HTMLResponse:
    """The signup page, or the dashboard if they are already signed in."""
    if account is not None:
        return RedirectResponse(HOME, status_code=303)  # type: ignore[return-value]

    return templates.TemplateResponse(
        request,
        "auth/signup.html",
        {"min_password_length": MIN_PASSWORD_LENGTH},
    )


@router.post("/signup", response_class=HTMLResponse)
def signup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    shop_name: str = Form(...),
    whatsapp_number: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    Create the account and sign them straight in.

    No confirmation email standing between a seller and the product. Email
    verification earns its place when there is something worth protecting
    behind it; right now it is a wall in front of an empty dashboard.

    Returns:
        A 303 to the dashboard with the session cookie set, or the form
        re-rendered at 400 with the error and the typed values.
    """
    try:
        account = create_account(
            db,
            email=email,
            password=password,
            shop_name=shop_name,
            whatsapp_number=whatsapp_number,
        )
    except SignupError as exc:
        # 400, not 200: the request genuinely failed, and saying so keeps the
        # logs and any future API client honest.
        return templates.TemplateResponse(
            request,
            "auth/signup.html",
            {
                "error": str(exc),
                # Echoed back so nothing has to be retyped. The password
                # deliberately is not — a password in re-rendered HTML ends up
                # in proxy logs and browser caches.
                "email": email,
                "shop_name": shop_name,
                "whatsapp_number": whatsapp_number,
                "min_password_length": MIN_PASSWORD_LENGTH,
            },
            status_code=400,
        )

    db.commit()

    response = RedirectResponse(HOME, status_code=303)
    set_session_cookie(response, account.id)
    return response  # type: ignore[return-value]


# ── Sign in ──────────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    next: str | None = None,
    account: Account | None = Depends(optional_account),
) -> HTMLResponse:
    """The login page, or the dashboard if they already have a session."""
    if account is not None:
        return RedirectResponse(_safe_next(next), status_code=303)  # type: ignore[return-value]

    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {
            # Sanitised before it reaches the template, so the hidden field can
            # never carry an off-site URL back to us.
            "next_url": next if _safe_next(next) == next else None,
            "notice": "You have been signed out." if "logged_out" in request.query_params else None,
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    password: str = Form(...),
    identifier: str = Form(""),
    email: str = Form(""),
    next: str | None = Form(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    Verify credentials and start a session.

    Returns:
        A 303 onward with the cookie set, or the form re-rendered at 401.

    Notes:
        ``AuthError`` carries one message for every cause — unknown email,
        wrong password, deactivated account. Rendering ``str(exc)`` rather than
        a message of our own keeps it that way: there is exactly one place that
        decides what a failed login says, and it is the service.
    """
    # The form field was renamed from `email` to `identifier` when a WhatsApp
    # number became a valid sign-in. Both are accepted so a cached page or an
    # old bookmark does not 422 on somebody mid-login.
    typed = (identifier or email).strip()

    try:
        account = authenticate(db, identifier=typed, password=password)
    except AuthError as exc:
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {
                "error": str(exc),
                "email": typed,
                "identifier": typed,
                "next_url": next if _safe_next(next) == next else None,
            },
            status_code=401,
        )

    # authenticate() writes last_login_at and may rehash the password to
    # stronger parameters. Both are lost without this.
    db.commit()

    response = RedirectResponse(_safe_next(next), status_code=303)
    set_session_cookie(response, account.id)
    return response  # type: ignore[return-value]


# ── Sign out ─────────────────────────────────────────────────────────────────


@router.post("/logout")
def logout() -> RedirectResponse:
    """
    End the session.

    POST only. A GET logout can be fired by any ``<img>`` tag on any site,
    which is a cheap way to log someone out mid-task for no reason.
    """
    response = RedirectResponse("/login?logged_out=1", status_code=303)
    clear_session_cookie(response)
    return response
