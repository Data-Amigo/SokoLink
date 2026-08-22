"""
Setting up how a seller gets paid.

This is the screen that decides whether a shop can take money at all — until a
payment method exists, ``place_order`` refuses and the storefront is a catalogue
nobody can buy from.

What is defended here:

    Pochi never carries STK credentials      Daraja cannot push to it
    credentials are all-or-nothing           three of four fails at the handset
    blank means unchanged, never deleted     the form cannot show a secret back
    secrets never render                     not in the page, not in a value=""
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Account, PaymentMethodKind
from app.secrets_vault import decrypt
from app.services.accounts import create_account
from app.services.payment_methods import (
    PaymentSetupError,
    disable_stk,
    save_payment_method,
)
from tests.factories import make_seller

PASSWORD = "correct-horse-battery"

FULL_STK = {
    "stk_shortcode": "174379",
    "consumer_key": "ckey",
    "consumer_secret": "csecret",
    "passkey": "pkey",
}


def signed_in(client: TestClient, db: Session, email: str = "seller@example.com") -> Account:
    account = create_account(db, email=email, password=PASSWORD, shop_name="Nairobi Thrift")
    db.flush()
    client.post("/login", data={"email": email, "password": PASSWORD})
    return account


class TestSavingAMethod:
    def test_pochi_needs_only_a_phone_number(self, db: Session) -> None:
        """
        The baseline case, and the one most sellers use. Nothing to apply for,
        nothing from Safaricom.
        """
        seller = make_seller(db)
        method = save_payment_method(db, seller, kind="pochi", number="0712 345 678")

        assert method.kind == PaymentMethodKind.POCHI.value
        assert method.number == "254712345678"
        assert method.can_stk is False

    def test_a_till_number_is_kept_exactly_as_issued(self, db: Session) -> None:
        """Reformatting it would show the seller a number they do not recognise
        from their own statement."""
        seller = make_seller(db)
        method = save_payment_method(db, seller, kind="till", number="832145")

        assert method.number == "832145"

    def test_a_malformed_pochi_number_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(PaymentSetupError, match="Safaricom number"):
            save_payment_method(db, seller, kind="pochi", number="12")

    def test_a_malformed_shortcode_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(PaymentSetupError, match="digits"):
            save_payment_method(db, seller, kind="till", number="12")

    def test_saving_twice_replaces_rather_than_duplicates(self, db: Session) -> None:
        """
        A seller changing their till expects to change it, not to be told they
        already have one. The unique constraint would refuse a second row anyway.
        """
        seller = make_seller(db)
        first = save_payment_method(db, seller, kind="pochi", number="0712345678")
        second = save_payment_method(db, seller, kind="till", number="832145")

        assert first.id == second.id
        assert second.kind == PaymentMethodKind.TILL.value


class TestCredentials:
    def test_pochi_cannot_take_stk_credentials(self, db: Session) -> None:
        """
        Daraja cannot push to Pochi. Storing them would create a row that looks
        STK-capable and fails with a buyer waiting.
        """
        seller = make_seller(db)
        with pytest.raises(PaymentSetupError, match="cannot receive"):
            save_payment_method(db, seller, kind="pochi", number="0712345678", **FULL_STK)

    def test_partial_credentials_are_refused(self, db: Session) -> None:
        """Three of four fails at the STK call — the worst place to find out."""
        seller = make_seller(db)
        with pytest.raises(PaymentSetupError, match="all four"):
            save_payment_method(
                db,
                seller,
                kind="till",
                number="832145",
                stk_shortcode="174379",
                consumer_key="ckey",
            )

    def test_full_credentials_enable_stk_and_are_encrypted(self, db: Session) -> None:
        seller = make_seller(db)
        method = save_payment_method(db, seller, kind="till", number="832145", **FULL_STK)

        assert method.can_stk is True
        # Stored encrypted, and recoverable — we have to send them to Daraja.
        assert method.passkey_enc is not None
        assert method.passkey_enc != "pkey"
        assert decrypt(method.passkey_enc) == "pkey"

    def test_blank_fields_leave_saved_credentials_alone(self, db: Session) -> None:
        """
        The form never shows a secret back, so an empty box means "unchanged".
        Treating it as "delete" would silently disable a seller's STK the first
        time they corrected a typo in their till number.
        """
        seller = make_seller(db)
        save_payment_method(db, seller, kind="till", number="832145", **FULL_STK)

        method = save_payment_method(db, seller, kind="till", number="999888")

        assert method.number == "999888"
        assert method.can_stk is True
        assert decrypt(method.passkey_enc or "") == "pkey"

    def test_switching_to_pochi_clears_credentials(self, db: Session) -> None:
        """The database refuses to hold them, and they are no longer usable."""
        seller = make_seller(db)
        save_payment_method(db, seller, kind="till", number="832145", **FULL_STK)

        method = save_payment_method(db, seller, kind="pochi", number="0712345678")

        assert method.can_stk is False
        assert method.passkey_enc is None
        assert method.stk_shortcode is None

    def test_credentials_can_be_withdrawn(self, db: Session) -> None:
        """
        Custody should be reversible. A seller who changes their mind goes back
        to confirming by hand without losing their shop.
        """
        seller = make_seller(db)
        save_payment_method(db, seller, kind="till", number="832145", **FULL_STK)

        method = disable_stk(db, seller)

        assert method is not None
        assert method.can_stk is False
        assert method.number == "832145"


class TestTheScreen:
    def test_it_is_behind_the_login_wall(self, client: TestClient, db: Session) -> None:
        """The page a seller's payment credentials are typed into."""
        response = client.get("/settings/payment", follow_redirects=False)
        assert response.status_code in (302, 303, 307)
        assert "/login" in response.headers["location"]

    def test_a_new_seller_is_told_they_cannot_take_orders(
        self, client: TestClient, db: Session
    ) -> None:
        """A shop that looks finished and cannot be bought from is worse than
        one that says it is not ready."""
        signed_in(client, db)
        body = client.get("/settings/payment").text
        assert "cannot take orders yet" in body

    def test_saving_through_the_form_works(self, client: TestClient, db: Session) -> None:
        account = signed_in(client, db)

        response = client.post(
            "/settings/payment",
            data={"kind": "pochi", "number": "0712345678", "account_name": "Nairobi Thrift"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        db.refresh(account)
        assert account.seller is not None
        assert account.seller.payment_method is not None
        assert account.seller.payment_method.number == "254712345678"

    def test_a_saved_secret_is_never_rendered_back(self, client: TestClient, db: Session) -> None:
        """
        THE ONE THAT MATTERS MOST HERE. There must be no path from the database
        to a rendered passkey — not in text, not in a value attribute.
        """
        account = signed_in(client, db)
        seller = account.seller
        assert seller is not None
        save_payment_method(db, seller, kind="till", number="832145", **FULL_STK)

        body = client.get("/settings/payment").text

        assert "pkey" not in body
        assert "csecret" not in body
        assert "ckey" not in body
        # The page still says they exist, so the seller knows they are set.
        assert "saved" in body.lower()
