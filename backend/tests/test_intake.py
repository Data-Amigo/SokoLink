"""
A forwarded catalogue post becomes a draft product.

THE PRODUCT'S CENTRAL PROMISE. The catalogue is not scraped and not typed: it
already exists, written by the seller, in the WhatsApp groups they already sell
in. Everything else in the system serves what lands here.

THE MODEL IS ALWAYS FAKED. Tests never hit a paid API — that is not a
preference, it is the difference between a suite you can run on every commit and
one nobody runs. The fake is a plain object satisfying the same call, which also
means these tests fail for real reasons rather than for quota ones.

WHAT IS ACTUALLY BEING PROVEN. Not that Gemini can read an image — that is
Google's problem and unfalsifiable here. These prove the rules AROUND the model:

    a price is copied only when the model saw one
    nothing is ever published
    an image is paid for once ever, including across failures
    a seller is recognised; a buyer is not
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ParsedMedia, Product, ProductStatus, Seller
from app.schemas.draft import ProductDraft
from app.services import intake, media
from app.services.bot import find_seller_by_phone, handle
from tests.factories import make_seller

MEDIA_ID = "SM123:0"

#: Only the Twilio downloader still takes a URL; everything above it now takes
#: a callable, because Meta media is fetched by id through two Graph calls.
TWILIO_MEDIA_URL = "https://api.twilio.com/media/ME123"
IMAGE = bytes([0xFF, 0xD8]) + b"fake-jpeg-bytes"


def fetch() -> bytes:
    """Stands in for whichever provider delivered the image. The point of the
    callable seam: intake owns the cache and the rules, never the transport."""
    return IMAGE


def a_draft(**overrides: Any) -> ProductDraft:
    """A plausible thing the model might return."""
    values: dict[str, Any] = {
        "is_product": True,
        "name": "Ankara Print Shirt",
        "description": "Cotton shirt, Nairobi made.",
        "price_kes": None,
        "unit_quantity": None,
        "unit_label": None,
        "price_evidence": None,
        "confidence": 0.4,
    }
    values.update(overrides)
    return ProductDraft.model_validate(values)


class FakeAgent:
    """Stands in for Gemini. Records what it was asked."""

    def __init__(self, draft: ProductDraft | None = None, error: Exception | None = None) -> None:
        self._draft = draft or a_draft()
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def draft_from_forwarded(self, caption: str, image_bytes: bytes) -> ProductDraft:
        self.calls.append((caption, len(image_bytes)))
        if self._error:
            raise self._error
        return self._draft


@pytest.fixture(autouse=True)
def covers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Every test in this module writes its photos to a temporary directory.

    AUTOUSE, DELIBERATELY. Intake now stores the forwarded image, so any test
    that ingests writes a file. Without this the suite would slowly fill the
    repository's own media directory with fake JPEGs, and the tests that count
    what is stored would pass or fail depending on what a previous run left
    behind.
    """
    directory = tmp_path / "covers"
    monkeypatch.setattr(media, "COVERS_DIR", directory)
    return directory


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> FakeAgent:
    """Swap the real agent out, and hand the fake to the test."""
    fake = FakeAgent()
    monkeypatch.setattr(intake, "get_draft_agent", lambda: fake)
    monkeypatch.setattr(intake, "download_media", lambda url: b"\xff\xd8fake-jpeg-bytes")
    return fake


class TestWhatTheAgentProposes:
    def test_a_forwarded_post_becomes_a_draft_product(self, db: Session, agent: FakeAgent) -> None:
        seller = make_seller(db)

        result = intake.ingest_forwarded_post(
            db, seller, media_id=MEDIA_ID, fetch=fetch, caption="Fresh stock"
        )

        assert result.product.title == "Ankara Print Shirt"
        assert result.product.status == ProductStatus.DRAFT.value
        assert agent.calls == [("Fresh stock", len(b"\xff\xd8fake-jpeg-bytes"))]

    def test_it_is_never_published(self, db: Session, agent: FakeAgent) -> None:
        """
        PUBLISH IS A HUMAN GATE. An ingestion path that could publish would be a
        second way to go live that the review queue never sees — and the seller
        would find out from a buyer.
        """
        seller = make_seller(db)

        result = intake.ingest_forwarded_post(
            db, seller, media_id=MEDIA_ID, fetch=fetch, caption=""
        )

        assert result.product.status == ProductStatus.DRAFT.value

    def test_a_price_the_model_saw_is_kept(self, db: Session, agent: FakeAgent) -> None:
        agent._draft = a_draft(price_kes=1800, price_evidence="bei 1800", confidence=0.9)
        seller = make_seller(db)

        result = intake.ingest_forwarded_post(
            db, seller, media_id=MEDIA_ID, fetch=fetch, caption=""
        )

        assert result.product.price_kes == 1800
        assert result.product.price_evidence == "bei 1800"
        assert result.needs_price is False

    def test_a_price_the_model_did_not_see_stays_missing(
        self, db: Session, agent: FakeAgent
    ) -> None:
        """
        THE MOST IMPORTANT RULE HERE. Verified against 24 real captions: zero
        mention KSh. A missing price costs the seller five seconds; a guessed
        one reaches a buyer.
        """
        seller = make_seller(db)

        result = intake.ingest_forwarded_post(
            db, seller, media_id=MEDIA_ID, fetch=fetch, caption=""
        )

        assert result.product.price_kes is None
        assert result.needs_price is True

    def test_a_bulk_lot_keeps_its_units(self, db: Session, agent: FakeAgent) -> None:
        """ "3000 for 30 pairs" is not a KSh 3,000 pair of shoes."""
        agent._draft = a_draft(price_kes=3000, unit_quantity=30, unit_label="pairs", confidence=0.9)
        seller = make_seller(db)

        result = intake.ingest_forwarded_post(
            db, seller, media_id=MEDIA_ID, fetch=fetch, caption="@3000 30pairs"
        )

        assert result.product.unit_quantity == 30
        assert result.product.unit_label == "pairs"

    def test_something_that_is_not_a_product_is_refused(
        self, db: Session, agent: FakeAgent
    ) -> None:
        agent._draft = a_draft(is_product=False, name="A selfie")
        seller = make_seller(db)

        with pytest.raises(intake.IntakeError):
            intake.ingest_forwarded_post(db, seller, media_id=MEDIA_ID, fetch=fetch, caption="")

        assert db.scalars(select(Product)).all() == []


class TestPaidOnce:
    """
    Inference is the only thing here that costs money, and the traffic is the
    worst case for it: a seller onboarding forwards their whole catalogue at
    once, and Twilio redelivers anything slow.
    """

    def test_the_same_image_is_read_once_ever(self, db: Session, agent: FakeAgent) -> None:
        for _ in range(4):
            intake.parse_once(db, MEDIA_ID, "Fresh stock", fetch)

        assert len(agent.calls) == 1

    def test_a_failure_is_cached_too(self, db: Session, agent: FakeAgent) -> None:
        """
        Without this, an image the model reliably chokes on is re-sent and
        re-billed on every redelivery.
        """
        from app.agent.draft import DraftAgentError

        agent._error = DraftAgentError("quota exhausted")

        for _ in range(3):
            with pytest.raises(intake.IntakeError):
                intake.parse_once(db, MEDIA_ID, "", fetch)

        assert len(agent.calls) == 1
        row = db.scalar(select(ParsedMedia).where(ParsedMedia.provider_media_id == MEDIA_ID))
        assert row is not None
        assert row.error is not None
        assert row.draft is None

    def test_a_different_image_is_read_again(self, db: Session, agent: FakeAgent) -> None:
        intake.parse_once(db, "SM1:0", "", fetch)
        intake.parse_once(db, "SM2:0", "", fetch)

        assert len(agent.calls) == 2


class TestDownloading:
    def test_a_non_image_is_refused_before_the_model_is_called(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Video arrives here too. It is a real tier in the cascade, but it is
        parked — pretending otherwise bills us for a call that cannot succeed.
        """
        fake = FakeAgent()
        monkeypatch.setattr(intake, "get_draft_agent", lambda: fake)
        monkeypatch.setattr(
            httpx,
            "get",
            lambda *a, **k: httpx.Response(
                200, headers={"content-type": "video/mp4"}, content=b"movie"
            ),
        )
        monkeypatch.setattr(get_settings(), "twilio_account_sid", "AC1")
        monkeypatch.setattr(get_settings(), "twilio_auth_token", "tok")

        with pytest.raises(intake.IntakeError, match="only read photos"):
            intake.download_media(TWILIO_MEDIA_URL)

        assert fake.calls == []

    def test_media_is_fetched_with_account_auth(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Twilio's media URLs are not public. An unauthenticated fetch returns a
        401 page, which as image bytes would reach the model as garbage and
        produce a confident draft of nothing.
        """
        seen: dict[str, Any] = {}

        def spy(url: str, **kwargs: Any) -> httpx.Response:
            seen.update(kwargs)
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"jpg")

        monkeypatch.setattr(httpx, "get", spy)
        monkeypatch.setattr(get_settings(), "twilio_account_sid", "AC1")
        monkeypatch.setattr(get_settings(), "twilio_auth_token", "tok")

        intake.download_media(TWILIO_MEDIA_URL)

        assert seen["auth"] == ("AC1", "tok")


class TestWhoIsTalking:
    """
    A message from a number that owns a shop is a SELLER. Nobody types a command
    to switch mode — a seller forwarding a photo means one thing, and asking
    them to announce it first is friction invented for our convenience.
    """

    def test_a_seller_is_recognised_by_their_own_number(self, db: Session) -> None:
        seller = make_seller(db, whatsapp_number="254712345678")

        assert find_seller_by_phone(db, "254712345678") is seller

    def test_a_number_stored_the_local_way_still_matches(self, db: Session) -> None:
        """A seller may have typed 07… in the workspace, and neither form is
        wrong to them."""
        seller = make_seller(db, whatsapp_number="0712345678")

        assert find_seller_by_phone(db, "254712345678") is seller

    def test_a_stranger_is_not_a_seller(self, db: Session) -> None:
        make_seller(db, whatsapp_number="254712345678")

        assert find_seller_by_phone(db, "254799999999") is None

    def test_a_photo_from_a_seller_creates_a_draft(self, db: Session, agent: FakeAgent) -> None:
        seller: Seller = make_seller(db, whatsapp_number="254712345678")

        outcome = handle(db, "254712345678", "Fresh stock", media=[(MEDIA_ID, fetch)])

        # The price question is a separate reply now: the forward confirms, then
        # the pricing queue asks by name.
        said = " ".join(r.body for r in outcome.replies)
        assert "Ankara Print Shirt" in said
        assert "price" in said.lower()
        product = db.scalars(select(Product).where(Product.seller_id == seller.id)).first()
        assert product is not None
        assert product.status == ProductStatus.DRAFT.value

    def test_a_photo_from_a_buyer_does_not(self, db: Session, agent: FakeAgent) -> None:
        """A buyer sending a picture is not forwarding a catalogue, and must
        never write to somebody's shop."""
        make_seller(db, whatsapp_number="254712345678")

        handle(db, "254799999999", "is this available?", media=[(MEDIA_ID, fetch)])

        assert agent.calls == []
        assert db.scalars(select(Product)).all() == []

    def test_several_photos_in_one_message_each_become_a_product(
        self, db: Session, agent: FakeAgent
    ) -> None:
        """A forwarded catalogue is often several images at once — which is why
        the webhook passes a list."""
        seller = make_seller(db, whatsapp_number="254712345678")

        handle(
            db,
            "254712345678",
            "New arrivals",
            media=[("SM9:0", fetch), ("SM9:1", fetch)],
        )

        products = db.scalars(select(Product).where(Product.seller_id == seller.id)).all()
        assert len(products) == 2


class TestThePhotoSurvives:
    """
    THE PHOTO IS THE POST. A seller forwards a catalogue post because the
    picture is what they sell with. For a long time intake downloaded the image,
    showed it to the model and dropped it — so every forwarded product reached
    the storefront as a card reading "No photo", and the seller's own photograph
    was the one thing the pipeline threw away.

    Meta's media URLs are signed and short-lived. There is no fetching it back
    later, which is why these tests are about the bytes being KEPT, not about
    them being retrievable.
    """

    def test_a_forwarded_photo_is_stored_and_attached(
        self, db: Session, agent: FakeAgent, covers: Path
    ) -> None:
        seller = make_seller(db)

        result = intake.ingest_forwarded_post(
            db, seller, media_id=MEDIA_ID, fetch=fetch, caption="Fresh stock"
        )

        assert result.product.cover_url is not None
        assert result.product.cover_url.startswith("covers/")

        stored = covers / Path(result.product.cover_url).name
        assert stored.read_bytes() == IMAGE

    def test_the_storefront_gets_a_url_it_can_render(self, db: Session, agent: FakeAgent) -> None:
        """The template asks for ``product.cover_url | media``; a stored path
        that does not survive that filter is the same bug wearing a hat."""
        seller = make_seller(db)

        result = intake.ingest_forwarded_post(
            db, seller, media_id=MEDIA_ID, fetch=fetch, caption=""
        )

        assert media.public_url(result.product.cover_url) == f"/media/{result.product.cover_url}"

    def test_a_redelivery_reuses_the_copy_it_already_kept(
        self, db: Session, agent: FakeAgent
    ) -> None:
        """
        The parse cache means a redelivered forward is never downloaded again,
        so the second pass holds no bytes. It must still find the photo — or
        Meta retrying a message would produce a product worse than the first.
        """
        seller = make_seller(db)

        first = intake.ingest_forwarded_post(db, seller, media_id=MEDIA_ID, fetch=fetch, caption="")

        def refuse() -> bytes:
            raise AssertionError("a cache hit must not download anything")

        second = intake.ingest_forwarded_post(
            db, seller, media_id=MEDIA_ID, fetch=refuse, caption=""
        )

        assert second.cached is True
        assert second.product.cover_url == first.product.cover_url

    def test_a_png_is_not_stored_as_a_jpeg(self, db: Session, agent: FakeAgent) -> None:
        """The extension becomes the Content-Type. A PNG served as image/jpeg
        renders in a browser — which is why it survives review — and then fails
        in the link-preview crawler that takes the header at its word."""
        seller = make_seller(db)
        png = b"\x89PNG\r\n\x1a\n" + b"fake"

        result = intake.ingest_forwarded_post(
            db, seller, media_id="PNG:0", fetch=lambda: png, caption=""
        )

        assert result.product.cover_url is not None
        assert result.product.cover_url.endswith(".png")

    def test_an_unwritable_store_still_produces_the_product(
        self, db: Session, agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing photo costs one card. A lost draft costs the seller the
        thing they forwarded, so a full disk must never reach them as an error."""
        seller = make_seller(db)
        monkeypatch.setattr(media, "store_image_bytes", lambda data, *, key: None)
        monkeypatch.setattr(intake, "store_image_bytes", lambda data, *, key: None)

        result = intake.ingest_forwarded_post(
            db, seller, media_id=MEDIA_ID, fetch=fetch, caption=""
        )

        assert result.product.cover_url is None
        assert result.product.status == ProductStatus.DRAFT.value
