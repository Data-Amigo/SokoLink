"""
The WhatsApp Flows data-exchange endpoint. PUBLIC, and encrypted end to end.

    POST /webhooks/flows   Meta posts encrypted screen requests here

WHY IT IS ITS OWN ROUTE, separate from the message webhook. The message webhook
is signed with an app secret over its raw body; this one is protected by
asymmetric encryption instead — only the holder of our private key can read a
request, and Meta only accepts a reply encrypted under the same key. The two
security models must not be tangled, so they live in two routes.

WHAT IT ANSWERS TODAY. Meta's health-check ``ping`` — which a dynamic Flow must
pass before it can be published — plus a valid encrypted reply for any other
call. Screen routing (categories → products → cart) is added in the next stage;
it slots into ``services/whatsapp_flows.dispatch`` without touching this route.

STATUS CODES ARE PART OF THE CONTRACT. A decryption failure returns 421 so Meta
refreshes our public key and retries — the documented recovery, not an error to
swallow. A missing key is 503: unconfigured, not broken.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.services.whatsapp_flows import (
    FlowEncryptionError,
    decrypt_request,
    dispatch,
    encrypt_response,
    route_screen,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["flows"])

#: Where Meta posts Flow data-exchange requests. Registered as the Flow's
#: endpoint URI, so a silent change here strands a published Flow.
FLOWS_ENDPOINT_PATH = "/webhooks/flows"


@router.post(FLOWS_ENDPOINT_PATH)
async def flows_data_exchange(request: Request, db: Session = Depends(get_db)) -> Response:
    """
    Decrypt one Flow request, decide the reply, and return it encrypted.

    Returns:
        200 with the base64 AES-GCM reply body Meta expects; 421 when a request
        cannot be decrypted (Meta refreshes the public key and retries); 503
        when no Flow private key is configured.
    """
    settings = get_settings()
    private_key = settings.whatsapp_flow_private_key
    if not private_key:
        return PlainTextResponse("Flow endpoint not configured", status_code=503)

    body: dict[str, Any] = await request.json()
    encrypted_flow_data = body.get("encrypted_flow_data")
    encrypted_aes_key = body.get("encrypted_aes_key")
    initial_vector = body.get("initial_vector")
    if not (encrypted_flow_data and encrypted_aes_key and initial_vector):
        # Not a shape we recognise — 421 asks Meta to re-handshake rather than
        # giving a stranger a 200 for an empty probe.
        return PlainTextResponse("Bad request", status_code=421)

    try:
        payload, aes_key, iv = decrypt_request(
            encrypted_flow_data,
            encrypted_aes_key,
            initial_vector,
            private_key,
            settings.whatsapp_flow_key_password,
        )
    except FlowEncryptionError:
        # 421 is the documented signal for "I could not decrypt" — Meta reloads
        # our public key and retries. Never log the ciphertext.
        logger.warning("Flow request failed to decrypt; returning 421 for key refresh")
        return PlainTextResponse("Decryption failed", status_code=421)

    # Meta's health check and error reports need no catalogue; screen requests
    # are served from the shop named in the flow_token.
    action = payload.get("action")
    data = payload.get("data") or {}
    if action == "ping" or (isinstance(data, dict) and data.get("error_message")):
        reply = dispatch(payload)
    else:
        reply = route_screen(db, payload)
        # Routes commit; get_db does not. Reads only here, but keep the contract.
        db.commit()

    return PlainTextResponse(encrypt_response(reply, aes_key, iv), status_code=200)
