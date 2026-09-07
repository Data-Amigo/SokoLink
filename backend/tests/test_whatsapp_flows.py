"""
The Flow data-exchange crypto, tested by standing in for Meta.

There is no WABA or device here, so the test BECOMES Meta: it generates a
keypair, encrypts a request the way Meta's servers do (AES-256-GCM + RSA-OAEP-
SHA256), and checks our endpoint decrypts it, dispatches, and encrypts the reply
with the IV bit-flipped — the exact contract a live Flow depends on.
"""

from __future__ import annotations

import base64
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.flows import FLOWS_ENDPOINT_PATH
from app.config import get_settings, settings
from app.models import ConversationState, Order, ProductStatus
from app.services import whatsapp_cloud
from app.services.bot import get_conversation, handle_flow_order
from app.services.bot.replies import Reply
from app.services.outbound import send_reply
from app.services.whatsapp_flows import (
    SCREEN_CART,
    SCREEN_CATEGORY,
    SCREEN_CHECKOUT,
    SCREEN_DETAIL,
    SCREEN_PRODUCTS,
    FlowEncryptionError,
    decrypt_request,
    dispatch,
    encrypt_response,
    first_screen_seed,
    mint_flow_token,
    parse_completion,
    route_screen,
    seller_from_flow_token,
)
from tests.factories import make_payment_method, make_product, make_seller


def _keypair() -> tuple[str, rsa.RSAPublicKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return pem, private_key.public_key()


def _meta_encrypts(
    payload: dict, public_key: rsa.RSAPublicKey
) -> tuple[str, str, str, bytes, bytes]:
    """Do exactly what Meta's servers do to a request body."""
    aes_key = os.urandom(32)
    iv = os.urandom(16)
    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(iv)).encryptor()
    ciphertext = encryptor.update(json.dumps(payload).encode("utf-8")) + encryptor.finalize()
    flow_data = ciphertext + encryptor.tag
    wrapped = public_key.encrypt(
        aes_key,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return b64(flow_data), b64(wrapped), b64(iv), aes_key, iv


def _meta_decrypts_response(response_b64: str, aes_key: bytes, iv: bytes) -> dict:
    """Do what Meta does to read our reply: same key, IV bit-flipped."""
    flipped = bytes(b ^ 0xFF for b in iv)
    raw = base64.b64decode(response_b64)
    ciphertext, tag = raw[:-16], raw[-16:]
    decryptor = Cipher(algorithms.AES(aes_key), modes.GCM(flipped, tag)).decryptor()
    return json.loads(decryptor.update(ciphertext) + decryptor.finalize())


class TestFlowCrypto:
    def test_ping_round_trip_reports_active(self) -> None:
        pem, public_key = _keypair()
        efd, eak, iv_b64, aes_key, iv = _meta_encrypts(
            {"version": "3.0", "action": "ping"}, public_key
        )

        payload, key, vector = decrypt_request(efd, eak, iv_b64, pem)
        reply = _meta_decrypts_response(
            encrypt_response(dispatch(payload), key, vector), aes_key, iv
        )

        assert reply == {"data": {"status": "active"}}

    def test_arbitrary_payload_decrypts_intact(self) -> None:
        pem, public_key = _keypair()
        original = {"version": "3.0", "action": "data_exchange", "screen": "MENU", "data": {"a": 1}}
        efd, eak, iv_b64, _, _ = _meta_encrypts(original, public_key)

        payload, _, _ = decrypt_request(efd, eak, iv_b64, pem)

        assert payload == original

    def test_tampered_ciphertext_is_rejected(self) -> None:
        pem, public_key = _keypair()
        efd, eak, iv_b64, _, _ = _meta_encrypts({"action": "ping"}, public_key)
        # Flip a byte in the ciphertext — GCM must refuse it.
        raw = bytearray(base64.b64decode(efd))
        raw[0] ^= 0x01
        tampered = base64.b64encode(bytes(raw)).decode("ascii")

        with pytest.raises(FlowEncryptionError):
            decrypt_request(tampered, eak, iv_b64, pem)

    def test_wrong_private_key_is_rejected(self) -> None:
        _, public_key = _keypair()
        other_pem, _ = _keypair()
        efd, eak, iv_b64, _, _ = _meta_encrypts({"action": "ping"}, public_key)

        with pytest.raises(FlowEncryptionError):
            decrypt_request(efd, eak, iv_b64, other_pem)


class TestFlowsEndpoint:
    def test_ping_through_the_http_endpoint(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pem, public_key = _keypair()
        monkeypatch.setenv("WHATSAPP_FLOW_PRIVATE_KEY", pem)
        get_settings.cache_clear()
        try:
            efd, eak, iv_b64, aes_key, iv = _meta_encrypts(
                {"version": "3.0", "action": "ping"}, public_key
            )
            resp = client.post(
                FLOWS_ENDPOINT_PATH,
                json={
                    "encrypted_flow_data": efd,
                    "encrypted_aes_key": eak,
                    "initial_vector": iv_b64,
                },
            )
            assert resp.status_code == 200
            assert _meta_decrypts_response(resp.text, aes_key, iv) == {"data": {"status": "active"}}
        finally:
            get_settings.cache_clear()

    def test_endpoint_is_503_without_a_configured_key(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("WHATSAPP_FLOW_PRIVATE_KEY", raising=False)
        get_settings.cache_clear()
        try:
            resp = client.post(
                FLOWS_ENDPOINT_PATH,
                json={"encrypted_flow_data": "x", "encrypted_aes_key": "y", "initial_vector": "z"},
            )
            assert resp.status_code == 503
        finally:
            get_settings.cache_clear()


def _shop_with_catalogue(db: Session):
    seller = make_seller(db, slug="kicks", is_published=True)
    make_product(
        db,
        seller,
        title="Leather Sandals",
        category="Shoes",
        status=ProductStatus.PUBLISHED.value,
        price_kes=1200,
        stock=5,
        platform_post_id="710000000000090001",
    )
    make_product(
        db,
        seller,
        title="Canvas Sneakers",
        category="Shoes",
        status=ProductStatus.PUBLISHED.value,
        price_kes=1800,
        stock=5,
        sizes=["40", "41"],
        platform_post_id="710000000000090002",
    )
    make_product(
        db,
        seller,
        title="Beaded Necklace",
        category="Beauty",
        status=ProductStatus.PUBLISHED.value,
        price_kes=800,
        stock=5,
        platform_post_id="710000000000090003",
    )
    return seller


def _tok(seller) -> str:
    return f"{seller.id}.nonce"


class TestFlowRouting:
    def test_init_returns_the_shops_categories(self, db: Session) -> None:
        seller = _shop_with_catalogue(db)
        reply = route_screen(db, {"action": "INIT", "flow_token": _tok(seller)})
        assert reply["screen"] == SCREEN_CATEGORY
        assert {c["title"] for c in reply["data"]["categories"]} == {"Shoes", "Beauty"}

    def test_category_returns_its_products(self, db: Session) -> None:
        seller = _shop_with_catalogue(db)
        reply = route_screen(
            db,
            {
                "screen": SCREEN_CATEGORY,
                "data": {"category": "Shoes", "cart": []},
                "flow_token": _tok(seller),
            },
        )
        assert reply["screen"] == SCREEN_PRODUCTS
        assert {p["title"] for p in reply["data"]["products"]} == {
            "Leather Sandals",
            "Canvas Sneakers",
        }

    def test_add_to_cart_uses_the_server_price_not_the_payload(self, db: Session) -> None:
        seller = _shop_with_catalogue(db)
        products = route_screen(
            db,
            {
                "screen": SCREEN_CATEGORY,
                "data": {"category": "Shoes", "cart": []},
                "flow_token": _tok(seller),
            },
        )["data"]["products"]
        sneakers_id = next(p["id"] for p in products if p["title"] == "Canvas Sneakers")

        detail = route_screen(
            db,
            {
                "screen": SCREEN_PRODUCTS,
                "data": {"product_id": sneakers_id, "cart": []},
                "flow_token": _tok(seller),
            },
        )
        assert detail["screen"] == SCREEN_DETAIL
        assert detail["data"]["has_sizes"] is True

        cart_screen = route_screen(
            db,
            {
                "screen": SCREEN_DETAIL,
                "data": {
                    "product_id": sneakers_id,
                    "variant": "41",
                    "quantity": 2,
                    "price_kes": 1,
                    "cart": [],
                },  # tampered price 1 must be ignored
                "flow_token": _tok(seller),
            },
        )
        assert cart_screen["screen"] == SCREEN_CART
        line = cart_screen["data"]["cart"][0]
        assert line["price_kes"] == 1800  # server price wins over the tampered 1
        assert line["qty"] == 2
        assert "3,600" in cart_screen["data"]["total"]

    def test_cart_checkout_moves_to_checkout(self, db: Session) -> None:
        seller = _shop_with_catalogue(db)
        reply = route_screen(
            db,
            {
                "screen": SCREEN_CART,
                "data": {
                    "next": "checkout",
                    "cart": [{"product_id": "1", "title": "X", "qty": 1, "price_kes": 800}],
                },
                "flow_token": _tok(seller),
            },
        )
        assert reply["screen"] == SCREEN_CHECKOUT
        assert "800" in reply["data"]["total"]

    def test_unknown_shop_token_serves_nothing(self, db: Session) -> None:
        reply = route_screen(db, {"action": "INIT", "flow_token": "999999.x"})
        assert reply["screen"] == SCREEN_CATEGORY
        assert reply["data"]["categories"] == []

    def test_seller_from_token_rejects_a_closed_shop(self, db: Session) -> None:
        seller = make_seller(db, slug="closed-shop", is_published=False)
        assert seller_from_flow_token(db, f"{seller.id}.x") is None


class TestFlowToken:
    def test_mint_names_the_shop_and_round_trips(self, db: Session) -> None:
        seller = _shop_with_catalogue(db)
        token = mint_flow_token(seller)
        assert token.startswith(f"{seller.id}.")
        assert token != mint_flow_token(seller)  # the nonce differs each time
        assert seller_from_flow_token(db, token) is seller

    def test_first_screen_seed_is_the_category_data(self, db: Session) -> None:
        seller = _shop_with_catalogue(db)
        seed = first_screen_seed(db, seller)
        assert seed["shop_name"] == seller.display_name
        assert {c["title"] for c in seed["categories"]} == {"Shoes", "Beauty"}
        assert seed["cart"] == [] and seed["cart_count"] == 0

    def test_parse_completion_tolerates_rubbish(self) -> None:
        assert parse_completion(None) == {}
        assert parse_completion("") == {}
        assert parse_completion("not json {") == {}
        assert parse_completion("[1, 2, 3]") == {}  # valid JSON, wrong shape
        assert parse_completion('{"name": "Amina"}') == {"name": "Amina"}


class TestFlowCompletion:
    """A finished Flow becomes a real order through the SAME checkout as the chat."""

    def _completion(self, seller, **over):
        products = route_screen(
            db=over.pop("db"),
            payload={
                "screen": SCREEN_CATEGORY,
                "data": {"category": "Shoes", "cart": []},
                "flow_token": _tok(seller),
            },
        )["data"]["products"]
        sneakers_id = next(p["id"] for p in products if p["title"] == "Canvas Sneakers")
        base = {
            "flow_token": _tok(seller),
            "cart": [
                {
                    "product_id": sneakers_id,
                    "title": "Canvas Sneakers",
                    "variant": "41",
                    "qty": 2,
                    "price_kes": 1,  # tampered — server price must win
                }
            ],
            "name": "Amina",
            "delivery": "collect",
        }
        base.update(over)
        return base

    def test_it_places_an_order_at_the_server_price(self, db: Session) -> None:
        seller = _shop_with_catalogue(db)
        make_payment_method(db, seller)
        completion = self._completion(seller, db=db)

        outcome = handle_flow_order(db, "254700111222", completion)

        order = db.query(Order).filter(Order.seller_id == seller.id).one()
        assert order.buyer_name == "Amina"
        assert order.buyer_phone == "254700111222"
        assert order.subtotal_kes == 3600  # 2 × 1800, not 2 × 1
        assert order.delivery_address is None  # collect, so no address
        convo = get_conversation(db, "254700111222")
        assert convo.state == ConversationState.PAYING
        assert convo.order_reference == order.reference
        # The buyer is told how to pay, and the seller is told it happened.
        assert outcome.replies
        assert outcome.notify

    def test_delivery_keeps_the_address(self, db: Session) -> None:
        seller = _shop_with_catalogue(db)
        make_payment_method(db, seller)
        completion = self._completion(
            seller, db=db, delivery="deliver", address="12 Biashara St, Nairobi"
        )

        handle_flow_order(db, "254700111333", completion)

        order = db.query(Order).filter(Order.seller_id == seller.id).one()
        assert order.delivery_address == "12 Biashara St, Nairobi"

    def test_an_unknown_shop_is_a_gentle_miss(self, db: Session) -> None:
        outcome = handle_flow_order(db, "254700111444", {"flow_token": "999999.x", "cart": []})
        assert outcome.replies
        assert not outcome.notify
        assert db.query(Order).count() == 0

    def test_a_sold_out_cart_charges_nothing(self, db: Session) -> None:
        seller = _shop_with_catalogue(db)
        make_payment_method(db, seller)
        completion = {
            "flow_token": _tok(seller),
            "cart": [{"product_id": "999999", "qty": 1}],  # not this shop's product
            "name": "Amina",
        }

        outcome = handle_flow_order(db, "254700111555", completion)

        assert db.query(Order).count() == 0
        assert "sold out" in outcome.replies[0].body.lower()


class TestFlowSend:
    """The buyer is offered the Flow, and it goes on the wire as a flow message."""

    def test_greeting_offers_the_flow_when_configured(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services.bot.presentation import _greeting

        seller = _shop_with_catalogue(db)
        monkeypatch.setattr(settings, "whatsapp_flow_id", "1234567890")
        replies = _greeting(db, seller)
        offers = [r for r in replies if r.flow is not None]
        assert len(offers) == 1
        flow_id, token, seed = offers[0].flow
        assert flow_id == "1234567890"
        assert token.startswith(f"{seller.id}.")
        assert seed["shop_name"] == seller.display_name

    def test_no_flow_offer_without_configuration(self, db: Session) -> None:
        from app.services.bot.presentation import _greeting

        seller = _shop_with_catalogue(db)
        assert all(r.flow is None for r in _greeting(db, seller))

    def test_outbound_sends_a_flow_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[tuple] = []
        monkeypatch.setattr(settings, "whatsapp_flow_id", "555")
        monkeypatch.setattr(
            whatsapp_cloud,
            "send_flow",
            lambda *a, **k: sent.append((a, k)) or "mid",
        )
        reply = Reply("Shop Nairobi Thrift", flow=("555", "7.abc", {"shop_name": "X"}))

        send_reply("254700000000", reply)

        assert len(sent) == 1
        args, _ = sent[0]
        assert args[0] == "254700000000"
        assert args[2] == "555"  # flow_id from settings
        assert args[3] == "7.abc"  # flow_token

    def test_outbound_falls_back_to_text_without_a_flow_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        texts: list[str] = []
        monkeypatch.setattr(settings, "whatsapp_flow_id", None)
        monkeypatch.setattr(whatsapp_cloud, "send_text", lambda to, body: texts.append(body) or "m")
        reply = Reply("Shop Nairobi Thrift", flow=("x", "7.abc", {}))

        send_reply("254700000000", reply)

        assert texts == ["Shop Nairobi Thrift"]


class TestFlowsEndpointCompletion:
    def test_nfm_reply_places_an_order_through_the_webhook(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hashlib
        import hmac

        # The webhook refuses to run without an app secret, and verifies the
        # signature over the raw body — so configure one and sign the post.
        app_secret = "an-app-secret"
        monkeypatch.setattr(get_settings(), "whatsapp_app_secret", app_secret)
        # No real sends during the webhook's post-commit delivery.
        monkeypatch.setattr(whatsapp_cloud, "send_text", lambda *a, **k: "m")
        monkeypatch.setattr(whatsapp_cloud, "send_buttons", lambda *a, **k: "m")
        seller = _shop_with_catalogue(db)
        make_payment_method(db, seller)
        products = route_screen(
            db,
            {
                "screen": SCREEN_CATEGORY,
                "data": {"category": "Shoes", "cart": []},
                "flow_token": _tok(seller),
            },
        )["data"]["products"]
        pid = next(p["id"] for p in products if p["title"] == "Leather Sandals")
        response_json = json.dumps(
            {
                "flow_token": _tok(seller),
                "cart": [{"product_id": pid, "qty": 1}],
                "name": "Juma",
                "delivery": "collect",
            }
        )
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.NFM1",
                                        "from": "254700999888",
                                        "type": "interactive",
                                        "interactive": {
                                            "type": "nfm_reply",
                                            "nfm_reply": {
                                                "name": "flow",
                                                "body": "Sent",
                                                "response_json": response_json,
                                            },
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        }

        raw = json.dumps(payload).encode()
        digest = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
        resp = client.post(
            "/webhooks/meta",
            content=raw,
            headers={
                "X-Hub-Signature-256": f"sha256={digest}",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200

        order = db.query(Order).filter(Order.seller_id == seller.id).one()
        assert order.buyer_name == "Juma"
        assert order.buyer_phone == "254700999888"
