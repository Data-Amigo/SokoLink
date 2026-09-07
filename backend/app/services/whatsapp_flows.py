"""
WhatsApp Flows data-exchange: the encrypted request/response with Meta.

    Meta ──(encrypted_flow_data, encrypted_aes_key, initial_vector)──▶ our endpoint
                                    │
                    decrypt_request(): RSA-OAEP unwraps the AES key,
                    AES-256-GCM decrypts the payload
                                    │
                    dispatch() decides the next screen's data
                                    │
    Meta ◀──(base64 AES-GCM, IV flipped)── encrypt_response()

WHY THIS FILE EXISTS AT ALL. A *dynamic* Flow — one whose screens are filled
from our catalogue rather than baked into the Flow JSON — calls a server
endpoint between screens. Meta encrypts every such call end to end, and will not
let a dynamic Flow be published until the endpoint answers a health-check
correctly. So the encryption is not an add-on; it is the price of entry, and it
lives here, isolated and tested, rather than smeared through a route.

THE SCHEME IS META'S, IMPLEMENTED TO THE LETTER (see their "Flows Encryption"
doc). A request carries three base64 blobs: the flow data (AES-256-GCM
ciphertext with the 16-byte tag appended), the AES key (RSA-OAEP-SHA256 wrapped
with OUR public key), and the initial vector. The response is encrypted with the
SAME AES key and the IV **bit-flipped** — Meta rejects a response that reuses the
IV unchanged, and a wrong flip is the most common reason a Flow endpoint "works"
in a unit test and fails against Meta.

THE PRIVATE KEY NEVER LEAVES THE SERVER, and is read from configuration, not the
database — a Postgres dump alone must not yield the ability to read buyers'
in-flight Flow data. The matching PUBLIC key is registered with Meta once.
"""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy.orm import Session

from app.models import Seller
from app.services.storefront import get_categories, get_public_product, get_public_products

#: The Flow's screen ids. They must match the screen names in the published
#: Flow JSON exactly — Meta routes by these strings, and a typo strands a screen.
SCREEN_CATEGORY = "CATEGORY"
SCREEN_PRODUCTS = "PRODUCTS"
SCREEN_DETAIL = "DETAIL"
SCREEN_CART = "CART"
SCREEN_CHECKOUT = "CHECKOUT"

#: AES-GCM authentication tag length, in bytes. Meta appends it to the
#: ciphertext; splitting it back off is required before decryption.
_GCM_TAG_BYTES = 16


class FlowEncryptionError(Exception):
    """
    A Flow request could not be decrypted, or a response could not be encrypted.

    Raised rather than returning None so a caller cannot mistake "we could not
    read this" for "this said nothing" — the endpoint answers a decryption
    failure with HTTP 421 so Meta refreshes the public key, which is the
    documented recovery and a different thing from an ordinary error.
    """


def _load_private_key(pem: str, password: str | None = None) -> Any:
    """
    Load the RSA private key that unwraps the AES key on every request.

    Args:
        pem: The PEM-encoded private key, as configured (never stored in the DB).
        password: The key's passphrase, if it was created with one.

    Returns:
        A cryptography private-key object.

    Raises:
        FlowEncryptionError: If the PEM (or passphrase) cannot be loaded — a
            misconfiguration that must fail loudly at first use, not silently
            reject every buyer.
    """
    try:
        return serialization.load_pem_private_key(
            pem.encode("utf-8"),
            password=password.encode("utf-8") if password else None,
        )
    except (ValueError, TypeError) as exc:
        raise FlowEncryptionError("Flow private key could not be loaded — check the PEM.") from exc


def decrypt_request(
    encrypted_flow_data_b64: str,
    encrypted_aes_key_b64: str,
    initial_vector_b64: str,
    private_key_pem: str,
    password: str | None = None,
) -> tuple[dict[str, Any], bytes, bytes]:
    """
    Decrypt one Flow data-exchange request from Meta.

    Args:
        encrypted_flow_data_b64: The AES-256-GCM ciphertext (tag appended), b64.
        encrypted_aes_key_b64: The AES key, RSA-OAEP-SHA256 wrapped, b64.
        initial_vector_b64: The GCM IV, b64.
        private_key_pem: Our RSA private key, PEM.
        password: The key's passphrase, if any.

    Returns:
        ``(payload, aes_key, iv)`` — the decoded JSON body, and the AES key and
        IV the RESPONSE must reuse (the IV bit-flipped). All three are needed by
        :func:`encrypt_response`, so they are returned together rather than
        re-derived.

    Raises:
        FlowEncryptionError: On any decryption failure — a tampered payload, the
            wrong key, or a malformed request.
    """
    private_key = _load_private_key(private_key_pem, password)

    try:
        flow_data = base64.b64decode(encrypted_flow_data_b64)
        iv = base64.b64decode(initial_vector_b64)
        encrypted_aes_key = base64.b64decode(encrypted_aes_key_b64)

        aes_key = private_key.decrypt(
            encrypted_aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        # The GCM tag is the last 16 bytes; the rest is ciphertext.
        ciphertext, tag = flow_data[:-_GCM_TAG_BYTES], flow_data[-_GCM_TAG_BYTES:]
        decryptor = Cipher(algorithms.AES(aes_key), modes.GCM(iv, tag)).decryptor()
        decrypted = decryptor.update(ciphertext) + decryptor.finalize()
        payload: dict[str, Any] = json.loads(decrypted)
    except Exception as exc:  # noqa: BLE001 — any failure here is one class: undecryptable
        raise FlowEncryptionError("Flow request could not be decrypted.") from exc

    return payload, aes_key, iv


def encrypt_response(response: dict[str, Any], aes_key: bytes, iv: bytes) -> str:
    """
    Encrypt the endpoint's reply the exact way Meta expects to receive it.

    Args:
        response: The plain response object (e.g. ``{"screen": ..., "data": ...}``).
        aes_key: The SAME AES key the request arrived under.
        iv: The request's IV. It is BIT-FLIPPED here, because Meta requires the
            response to reuse the key with an inverted IV and rejects a verbatim
            reuse — the single most common cause of a Flow endpoint that passes
            its own tests yet fails live.

    Returns:
        Base64 of ``ciphertext || tag`` — the raw string body Meta reads back.
    """
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(flipped_iv)).encryptor()
    body = json.dumps(response).encode("utf-8")
    ciphertext = encryptor.update(body) + encryptor.finalize()
    return base64.b64encode(ciphertext + encryptor.tag).decode("ascii")


def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Decide the response for one decrypted Flow request.

    Meta sends an ``action`` telling the endpoint why it was called. This handles
    the two that exist before any screen logic:

      - ``ping`` — the health check. Meta sends it on save and periodically, and
        a dynamic Flow cannot be published until it is answered with
        ``{"data": {"status": "active"}}``. Answering it is Stage 1's whole job.
      - ``data_exchange`` / ``INIT`` — a real screen request. Screen routing
        (categories → products → cart) lands here in Stage 2; for now it returns
        a small acknowledgement so the shape is exercised end to end.

    A client error report (``data.error_message`` present) is acknowledged so
    Meta stops resending it, per the documented contract.

    Args:
        payload: The decrypted request body.

    Returns:
        The plain response object to encrypt and return.
    """
    action = payload.get("action")

    if action == "ping":
        return {"data": {"status": "active"}}

    # Meta reports a client-side error by setting data.error_message; the
    # documented reply is a bare acknowledgement so it is not resent forever.
    data = payload.get("data") or {}
    if isinstance(data, dict) and data.get("error_message"):
        return {"data": {"acknowledged": True}}

    # A real screen request that reached dispatch() without a db — the endpoint
    # routes those through route_screen() instead. This is only hit for shapes
    # with no db context, so it stays a harmless valid reply.
    return {"data": {"status": "ok"}}


# ── Stage 2: the browse-and-order screen routing ────────────────────────────
#
# A dynamic Flow asks this endpoint for each screen's data as the buyer moves
# through it. The buyer's shop is carried in the flow_token (minted when the Flow
# is sent — Stage 3); every screen reads the catalogue through the same public
# storefront queries the chat and web use, so the three surfaces cannot disagree
# about what is for sale. The cart is threaded through the Flow's own data
# payload, but each line's PRICE is looked up server-side and never trusted from
# the client — the same rule the rest of the app follows.


def mint_flow_token(seller: Seller) -> str:
    """
    Make the token that names this shop for a Flow session.

    It is ``<seller_id>.<nonce>``: the id lets :func:`seller_from_flow_token`
    resolve the shop on every screen request and on completion without trusting
    the client, and the random nonce makes one buyer's token useless for reading
    another's session — a token is not a secret to the shop, but it should not be
    guessable across buyers.
    """
    return f"{seller.id}.{secrets.token_urlsafe(9)}"


def first_screen_seed(db: Session, seller: Seller) -> dict[str, Any]:
    """
    The CATEGORY screen's data, for seeding the Flow message at send time.

    Sending this in the ``flow_action_payload`` means the Flow shows the shop's
    real categories the instant it opens, rather than a spinner while the first
    ``data_exchange`` round-trips. It is the SAME shape the endpoint would
    return, so the opening screen and every later one agree.
    """
    return _screen_categories(db, seller, [])["data"]


def parse_completion(response_json: str | None) -> dict[str, Any]:
    """
    Read the buyer's completed-Flow payload, tolerating a malformed one.

    Meta delivers the ``complete`` action to the message webhook as an
    ``nfm_reply`` whose ``response_json`` is a JSON STRING. A buyer cannot forge
    a shape we then trust — the cart is re-priced and the shop re-resolved from
    the ``flow_token`` inside it — so the only job here is to decode it without
    letting a bad string crash the webhook.

    Returns:
        The decoded object, or ``{}`` when it is missing or not valid JSON.
    """
    if not response_json:
        return {}
    try:
        decoded = json.loads(response_json)
    except (ValueError, TypeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    # A concrete dict[str, Any], not the Any json.loads hands back — the keys a
    # buyer's client sends are strings, and downstream reads them by name.
    return {str(key): value for key, value in decoded.items()}


def seller_from_flow_token(db: Session, flow_token: str) -> Seller | None:
    """
    The shop a Flow session belongs to, from its token.

    The token is minted as ``<seller_id>.<nonce>`` when the Flow is sent, so the
    endpoint can tell which shop's catalogue to serve without trusting anything
    the client puts in the screen data. A closed or deleted shop resolves to
    None, so a Flow whose shop went away ends rather than serving a ghost.
    """
    head = (flow_token or "").split(".", 1)[0]
    if not head.isdigit():
        return None
    seller = db.get(Seller, int(head))
    if seller is None or not seller.is_published or seller.archived_at is not None:
        return None
    return seller


def _cart_total(cart: list[dict[str, Any]]) -> int:
    return sum(int(line.get("price_kes", 0)) * int(line.get("qty", 1)) for line in cart)


def _money(amount: int) -> str:
    return f"KES {amount:,}"


def _screen_categories(db: Session, seller: Seller, cart: list[dict[str, Any]]) -> dict[str, Any]:
    categories = get_categories(db, seller)
    return {
        "screen": SCREEN_CATEGORY,
        "data": {
            "shop_name": seller.display_name,
            "categories": [{"id": c, "title": c} for c in categories],
            "cart": cart,
            "cart_count": sum(int(line.get("qty", 1)) for line in cart),
        },
    }


def _screen_products(
    db: Session, seller: Seller, category: str | None, cart: list[dict[str, Any]]
) -> dict[str, Any]:
    products = get_public_products(db, seller, category=category or None)
    return {
        "screen": SCREEN_PRODUCTS,
        "data": {
            "category": category or "All",
            "products": [
                {
                    "id": str(p.id),
                    "title": p.title,
                    "price": p.price_display or "",
                    # ``description`` is what the Flow's radio list shows under
                    # each title, so the price is visible while choosing.
                    "description": p.price_display or "",
                }
                for p in products
            ],
            "cart": cart,
        },
    }


def _screen_detail(
    db: Session, seller: Seller, product_id: int, cart: list[dict[str, Any]]
) -> dict[str, Any]:
    product = get_public_product(db, seller, product_id)
    if product is None:
        return _screen_products(db, seller, None, cart)
    return {
        "screen": SCREEN_DETAIL,
        "data": {
            "product_id": str(product.id),
            "title": product.title,
            "price": product.price_display or "",
            "sizes": [{"id": s, "title": s} for s in (product.sizes or [])],
            "has_sizes": bool(product.sizes),
            "cart": cart,
        },
    }


def _screen_cart(cart: list[dict[str, Any]]) -> dict[str, Any]:
    lines = [
        {
            "title": f"{line['title']}" + (f" ({line['variant']})" if line.get("variant") else ""),
            "detail": f"{line.get('qty', 1)} × {_money(int(line.get('price_kes', 0)))}",
        }
        for line in cart
    ]
    # A Flow cannot iterate a text array on screen, so the lines are also folded
    # into one summary string the CART screen shows in a single text block.
    lines_text = "\n".join(f"• {line['title']} — {line['detail']}" for line in lines)
    summary = lines_text or "Your cart is empty."
    return {
        "screen": SCREEN_CART,
        "data": {
            "items": lines,
            "summary": summary,
            "total": _money(_cart_total(cart)),
            "cart": cart,
        },
    }


def _screen_checkout(cart: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "screen": SCREEN_CHECKOUT,
        "data": {"total": _money(_cart_total(cart)), "cart": cart},
    }


def _add_to_cart(
    db: Session, seller: Seller, cart: list[dict[str, Any]], data: dict[str, Any]
) -> list[dict[str, Any]]:
    """Append the chosen item — price looked up server-side, never trusted."""
    raw_id = str(data.get("product_id") or "")
    product = get_public_product(db, seller, int(raw_id)) if raw_id.isdigit() else None
    if product is None or product.price_kes is None:
        return cart
    qty = data.get("quantity") or data.get("qty") or 1
    try:
        qty = max(1, int(qty))
    except (TypeError, ValueError):
        qty = 1
    variant = str(data.get("variant") or data.get("size") or "").strip()
    return [
        *cart,
        {
            "product_id": str(product.id),
            "title": product.title,
            "variant": variant,
            "qty": qty,
            "price_kes": int(product.price_kes),
        },
    ]


def route_screen(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Serve the next screen for one browse-and-order Flow request.

    Args:
        db: Session.
        payload: The decrypted request — ``action``, ``screen``, ``data`` and
            ``flow_token``.

    Returns:
        ``{"screen": <name>, "data": {...}}`` for the next screen. A request
        whose shop cannot be resolved is sent back to the category screen of
        nothing rather than served another shop's stock.

    Notes:
        THE CART IS THE CLIENT'S TO CARRY, THE PRICE IS OURS TO SET. The running
        cart rides in ``data.cart`` between screens, but :func:`_add_to_cart`
        re-reads each product's price from the catalogue, so a tampered payload
        cannot change what something costs. The final submit is a ``complete``
        action delivered to the message webhook as an ``nfm_reply`` — placing the
        order from it is Stage 3, not here.
    """
    seller = seller_from_flow_token(db, str(payload.get("flow_token", "")))
    action = payload.get("action")
    screen = payload.get("screen")
    data = payload.get("data") or {}
    cart: list[dict[str, Any]] = list(data.get("cart") or [])

    if seller is None:
        return {"screen": SCREEN_CATEGORY, "data": {"categories": [], "cart": [], "cart_count": 0}}

    # Flow just opened, or came back to the start.
    if action == "INIT" or screen is None or screen == SCREEN_CATEGORY and data.get("restart"):
        return _screen_categories(db, seller, cart)

    if screen == SCREEN_CATEGORY:
        return _screen_products(db, seller, data.get("category"), cart)
    if screen == SCREEN_PRODUCTS:
        raw_id = str(data.get("product_id") or "")
        return (
            _screen_detail(db, seller, int(raw_id), cart)
            if raw_id.isdigit()
            else (_screen_products(db, seller, data.get("category"), cart))
        )
    if screen == SCREEN_DETAIL:
        return _screen_cart(_add_to_cart(db, seller, cart, data))
    if screen == SCREEN_CART:
        if str(data.get("next") or data.get("action") or "").lower() == "checkout":
            return _screen_checkout(cart)
        return _screen_categories(db, seller, cart)  # "keep shopping"

    return _screen_categories(db, seller, cart)
