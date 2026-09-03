"""
Health endpoints — liveness and readiness.

    GET /health        is the process alive?          (no dependencies)
    GET /health/ready  can it actually serve traffic? (checks Postgres)

WHY two endpoints rather than one: they answer different questions and have
different consequences. Liveness failing means "restart me". Readiness failing
means "stop sending me traffic, but do not restart — the database is down and
restarting will not fix that". Collapsing them into one endpoint makes a
platform restart-loop an app whose only problem is a slow database.

Railway polls these to decide whether a deploy succeeded.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import DEV_SECRET_KEY, settings
from app.db import get_db

router = APIRouter(tags=["health"])


class HealthOut(BaseModel):
    """Liveness response."""

    status: Literal["ok"]
    app: str
    env: str
    #: The commit actually running. See Settings.version for why it is here.
    version: str


class ReadyOut(BaseModel):
    """Readiness response. ``detail`` is only populated on failure."""

    status: Literal["ready", "degraded"]
    database: Literal["up", "down"]
    detail: str | None = None


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """
    Liveness: the process is running and can answer HTTP.

    Deliberately touches nothing external. If this fails, the process itself is
    broken and a restart is the correct response.
    """
    return HealthOut(
        status="ok",
        app=settings.app_name,
        env=settings.app_env,
        version=settings.version,
    )


class IntegrationsOut(BaseModel):
    """
    Which integrations this deployment can actually use.

    BOOLEANS, NEVER VALUES. This endpoint exists to be curled, screenshotted and
    pasted into chats while debugging a deploy — the moment it echoed a secret
    it would become the fastest way to leak one.

    IT REPORTS CONFIGURATION, NOT HEALTH. "receive_messages: true" means the
    variables are present, not that Meta is reachable or the token is valid.
    That distinction matters: a wrong token and a missing one look identical
    from the outside, and only one of them is fixed by adding a variable.
    """

    version: str
    env: str

    #: The webhook can verify Meta's signature and answer the handshake.
    receive_messages: bool
    #: The bot can reply. Without this, messages arrive and nothing goes back.
    send_messages: bool
    #: Forwarded photos can be read into draft products.
    read_photos: bool
    #: Templates can be created and listed — needed once, not to send.
    manage_templates: bool
    #: Session cookies are signed with a real key rather than the placeholder.
    secret_key_set: bool


@router.get("/health/integrations", response_model=IntegrationsOut)
def integrations() -> IntegrationsOut:
    """
    What this deployment is configured to do.

    Returns:
        One boolean per capability, and the running commit.

    Notes:
        WHY THIS IS WORTH AN ENDPOINT. "Which variables did that container
        actually receive" has been the question behind two long debugging
        sessions here, and both times it was answered by inference. A deploy
        that can be asked directly ends the guessing — and unlike reading a
        dashboard, it reports what the RUNNING PROCESS sees, which is the only
        thing that decides behaviour.
    """
    return IntegrationsOut(
        version=settings.version,
        env=settings.app_env,
        receive_messages=bool(settings.whatsapp_app_secret and settings.whatsapp_verify_token),
        send_messages=bool(settings.whatsapp_access_token and settings.whatsapp_phone_number_id),
        read_photos=bool(settings.gemini_api_key),
        manage_templates=bool(settings.whatsapp_business_account_id),
        secret_key_set=settings.secret_key != DEV_SECRET_KEY,
    )


@router.get("/health/ready", response_model=ReadyOut)
def ready(response: Response, db: Session = Depends(get_db)) -> ReadyOut:
    """
    Readiness: the app can reach Postgres and therefore actually serve traffic.

    Returns 503 when the database is unreachable so the platform stops routing
    traffic here. The error is logged and returned rather than raised: this
    endpoint's job is to *report* the failure, and crashing on it would hide the
    exact condition it exists to surface.
    """
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyOut(status="degraded", database="down", detail=str(exc))

    return ReadyOut(status="ready", database="up")
