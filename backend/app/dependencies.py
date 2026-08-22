"""
The session layer — who is asking, on every request.

    request ──> cookie ──> read_session_token ──> Account
                  │
                  └── missing/expired ──> LoginRequired ──> 303 to /login?next=…

WHY A REDIRECT AND NOT A 401. Everything above this is a browser, not an API
client. A 401 leaves a seller staring at ``{"detail":"Not authenticated"}`` with
no way forward. A redirect carrying ``?next=`` puts them on the login page and
then back where they were going — which is the difference between a product and
a demo.

WHY THE COOKIE IS SET HERE and not in the auth routes: ``Secure`` must be on in
production and off on localhost, ``HttpOnly`` and ``SameSite`` must never be
forgotten, and the max-age must match the token's own expiry. Four things that
have to agree, so they are written once.

The token itself — minting, signing, expiry — lives in ``app.security``. This
module is only the HTTP wrapper around it.
"""

from __future__ import annotations

from fastapi import Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Account, Seller
from app.security import (
    SESSION_COOKIE,
    SESSION_LIFETIME,
    AuthError,
    create_session_token,
    read_session_token,
)
from app.services.accounts import get_account


class LoginRequired(Exception):
    """
    Raised by :func:`current_account` when nobody is signed in.

    Carries where the user was trying to go, so the login page can return them
    there. Handled application-wide in ``main.py`` — a route never catches this.
    """

    def __init__(self, next_url: str | None = None) -> None:
        self.next_url = next_url
        super().__init__("Login required")


def login_redirect(next_url: str | None = None) -> RedirectResponse:
    """
    Build the redirect that sends an anonymous visitor to sign in.

    Args:
        next_url: The path they were trying to reach, echoed back as ``?next=``.
            Ignored unless it is a same-site absolute path — see the note.

    Returns:
        A 303 to the login page.

    Notes:
        **Only paths beginning with a single ``/`` are echoed.** Reflecting an
        arbitrary ``next`` would turn our login page into an open redirect: a
        phishing link could send someone to a genuine biasharamall.com login
        and then bounce them to an attacker's clone with the credentials still
        fresh in their mind. ``//evil.com`` is a protocol-relative URL, which
        is why the second character is checked too.
    """
    target = "/login"
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        from urllib.parse import quote

        target = f"/login?next={quote(next_url, safe='')}"
    # 303 rather than 302: it guarantees the browser re-issues as GET, so a
    # POST that hit an expired session does not get replayed after login.
    return RedirectResponse(target, status_code=303)


def set_session_cookie(response: Response, account_id: int) -> None:
    """
    Sign this account into the browser.

    Args:
        response: The response carrying the redirect after login or signup.
        account_id: Who is now signed in.

    Notes:
        ``Secure`` follows the environment rather than being hardcoded: on in
        production, off in dev — because a Secure cookie is silently dropped
        over plain http, and "login appears to work but nothing is remembered"
        is a miserable afternoon.

        ``SameSite=Lax`` is also our CSRF defence for now. It stops the cookie
        riding along on a cross-site POST, which is the shape every form on
        this app takes. When a genuine cross-site flow appears — an OAuth
        callback that must post back — this needs a real CSRF token instead.
    """
    response.set_cookie(
        key=SESSION_COOKIE,
        value=create_session_token(account_id),
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,  # no script can read it, so XSS cannot steal the session
        samesite="lax",
        secure=settings.is_prod,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """
    Sign out.

    The attributes must match those used when setting it, or the browser treats
    it as a different cookie and quietly keeps the old one.
    """
    response.delete_cookie(
        key=SESSION_COOKIE,
        httponly=True,
        samesite="lax",
        secure=settings.is_prod,
        path="/",
    )


def optional_account(
    request: Request,
    db: Session = Depends(get_db),
) -> Account | None:
    """
    Whoever is signed in, or None.

    For pages that render either way — a landing page that says "Dashboard"
    instead of "Sign in" when you already have a session.

    Returns:
        The account, or None if there is no cookie, the token is invalid or
        expired, or the account has since been deactivated. All of those are
        the same thing from a template's point of view: nobody is signed in.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None

    try:
        account_id = read_session_token(token)
    except AuthError:
        # A tampered or expired cookie is not an error worth surfacing; it is
        # simply an anonymous visitor. The stale cookie is cleared on their
        # next login rather than mutating a response we do not own here.
        return None

    # Re-checked on every request, not trusted from the token: an account
    # deactivated five minutes ago must lose access now, not in fourteen days.
    return get_account(db, account_id)


def current_account(
    request: Request,
    account: Account | None = Depends(optional_account),
) -> Account:
    """
    The signed-in account, or a redirect to login.

    Use on every page behind the login wall::

        @router.get("/dashboard")
        def dashboard(account: Account = Depends(current_account)): ...

    Raises:
        LoginRequired: When nobody is signed in. Converted to a 303 by the
            handler registered in ``main.py``.
    """
    if account is None:
        raise LoginRequired(next_url=request.url.path)
    return account


def current_seller(account: Account = Depends(current_account)) -> Seller:
    """
    The signed-in account's shop.

    Args:
        account: Resolved by :func:`current_account`, which redirects anonymous
            visitors before this runs.

    Returns:
        The seller, non-optional — which is the point. ``Account.seller`` is
        typed ``Seller | None`` because the foreign key allows it, so every
        route that touched it needed its own ``assert``. One dependency states
        the invariant once and every caller gets a plain ``Seller``.

    Raises:
        RuntimeError: If an account somehow has no shop. ``create_account``
            builds both in one transaction — either both exist or neither does
            — so this is unreachable by any supported path, and a loud 500 is
            the honest response to a broken invariant rather than a redirect
            that pretends the user did something wrong.
    """
    if account.seller is None:
        raise RuntimeError(
            f"Account {account.id} has no seller. Signup creates both in one "
            "transaction, so this row predates that guarantee or was made by hand."
        )
    return account.seller
