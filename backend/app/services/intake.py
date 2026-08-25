"""
A forwarded catalogue post becomes a draft product.

    WhatsApp media ──▶ download ──▶ [cache] ──▶ draft agent ──▶ DRAFT product
                                       │
                                       └── parsed once ever, by media id

THIS IS THE PRODUCT'S CENTRAL PROMISE. The catalogue is not scraped and not
typed: it already exists, written by the seller, in the WhatsApp groups they
already sell in. They forward it and we read it. Everything else in the system
exists to serve what lands here.

THE AGENT PROPOSES; THIS CODE DISPOSES. The model returns a name, a category,
sizes and — only when it can literally see one — a price. This module writes a
DRAFT and nothing else: it never publishes, never sets stock beyond the default,
and never invents a price the model did not report. The seller reviews it in the
workspace, which is the human gate the whole design rests on.

PARSED ONCE EVER, KEYED BY MEDIA ID. Inference is the only thing here that costs
money, and the traffic is the worst case for it — a seller onboarding forwards
their whole catalogue at once, and Twilio redelivers anything slow. The cache is
a table rather than a memo so it survives restarts and redeploys.

A MISSING PRICE IS A RESULT, NOT A FAILURE. Verified against 24 real captions:
zero mention KSh. Sellers withhold the price deliberately — that is the buyer
bottleneck this product removes — so the common case is a draft that needs one
number from the seller before it can go live.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.draft import DraftAgentError, get_draft_agent
from app.config import settings
from app.models import (
    IngestMethod,
    ParsedMedia,
    Platform,
    PriceSource,
    Product,
    ProductStatus,
    Seller,
)
from app.schemas.draft import ProductDraft

#: Twilio serves media from its own CDN behind account auth. Generous, because
#: a seller on a Nairobi mobile network has already uploaded this once and a
#: timeout here costs the whole forward.
DOWNLOAD_TIMEOUT_SECONDS = 30.0

#: Refuse anything larger. A catalogue photo is well under this; a bigger file
#: is either a video we cannot read yet or something that will cost real money
#: to send to a vision model for no gain.
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class IntakeError(RuntimeError):
    """
    A forwarded post could not be turned into a draft.

    Carries a seller-facing reason, because the reply in the chat is the only
    feedback they get and "something went wrong" tells them nothing about
    whether to retry, crop the image, or type it in by hand.
    """


@dataclass(frozen=True)
class IntakeResult:
    """What one forwarded image produced."""

    product: Product
    draft: ProductDraft
    #: True when the answer came from the cache rather than the model. Worth
    #: surfacing: it is the difference between a paid call and a free one.
    cached: bool

    @property
    def needs_price(self) -> bool:
        """Whether a seller must supply a price before this can be published."""
        return self.product.price_kes is None


def download_media(url: str) -> bytes:
    """
    Fetch one media file from the provider.

    Args:
        url: The provider's media URL, from the webhook payload.

    Returns:
        The bytes.

    Raises:
        IntakeError: On any failure, carrying something a seller can act on.

    Notes:
        AUTHENTICATED. Twilio's media URLs are not public — an unauthenticated
        fetch returns a 401 page, which as image bytes would reach the model as
        garbage and produce a confident draft of nothing.
    """
    sid, token = settings.twilio_account_sid, settings.twilio_auth_token
    if not (sid and token):
        raise IntakeError("WhatsApp media is not configured.")

    try:
        response = httpx.get(
            url, auth=(sid, token), timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        raise IntakeError("I couldn't download that image. Try sending it again.") from exc

    if response.status_code != 200:
        raise IntakeError("I couldn't download that image. Try sending it again.")

    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        # Video arrives here too. It is a real tier in the cascade, but it is
        # parked with the scrapers, and pretending otherwise would bill us for
        # a call that cannot succeed.
        raise IntakeError("I can only read photos right now — send a picture of the item.")

    if len(response.content) > MAX_IMAGE_BYTES:
        raise IntakeError("That image is too large. Try sending a smaller one.")

    return response.content


def parse_once(db: Session, media_id: str, caption: str, url: str) -> ProductDraft:
    """
    Read one forwarded image, or return what we already paid to learn.

    Args:
        db: Session.
        media_id: The provider's media id — the cache key.
        caption: The seller's own words from the message.
        url: Where to download it if we have not seen it before.

    Returns:
        The validated draft.

    Raises:
        IntakeError: When the image cannot be read. A failure is CACHED, so a
            redelivery of a post the model reliably chokes on does not bill us
            again for the same answer.
    """
    seen = db.scalar(select(ParsedMedia).where(ParsedMedia.provider_media_id == media_id))
    if seen is not None:
        if seen.error:
            raise IntakeError(seen.error)
        return ProductDraft.model_validate(seen.draft)

    image = download_media(url)

    try:
        draft = get_draft_agent().draft_from_forwarded(caption, image)
    except DraftAgentError as exc:
        reason = "I couldn't read that one. Try a clearer photo, or type the details."
        db.add(ParsedMedia(provider_media_id=media_id, error=reason))
        # FLUSHED, not merely added. The session runs with autoflush=False, so
        # an un-flushed row is invisible to the SELECT at the top of this
        # function — and this cache exists precisely to be read again moments
        # later, when Twilio redelivers the message that just timed out.
        db.flush()
        raise IntakeError(reason) from exc

    db.add(ParsedMedia(provider_media_id=media_id, draft=draft.model_dump(mode="json")))
    db.flush()
    return draft


def create_from_draft(db: Session, seller: Seller, draft: ProductDraft) -> Product:
    """
    Persist one draft as a product the seller can review.

    Args:
        db: Session.
        seller: Whose shop.
        draft: What the agent proposed.

    Returns:
        The created product, always DRAFT.

    Notes:
        PROVENANCE IS ``manual`` + ``upload``, which the database requires to
        agree. A forwarded post has no platform post id, so it can never look
        re-syncable — which matters, because a future feed sync must not
        overwrite something a seller sent us by hand.

        THE PRICE IS COPIED ONLY IF THE MODEL SAW ONE. There is no fallback, no
        estimate and no rounding. A wrong price reaches a buyer; a missing one
        costs the seller five seconds.
    """
    product = Product(
        seller_id=seller.id,
        title=draft.name,
        description=draft.description or None,
        platform=Platform.MANUAL.value,
        ingest_method=IngestMethod.UPLOAD.value,
        status=ProductStatus.DRAFT.value,
        price_kes=draft.price_kes,
        unit_quantity=draft.unit_quantity,
        unit_label=draft.unit_label,
        price_evidence=draft.price_evidence,
        price_source=PriceSource.COVER_IMAGE.value if draft.has_price else None,
        parse_confidence=draft.confidence,
    )
    db.add(product)
    db.flush()
    return product


def ingest_forwarded_post(
    db: Session, seller: Seller, *, media_id: str, media_url: str, caption: str
) -> IntakeResult:
    """
    Turn one forwarded catalogue post into a draft product.

    Args:
        db: Session. The caller commits.
        seller: Whose shop the product joins.
        media_id: The provider's media id, used as the parse cache key.
        media_url: Where to download the image.
        caption: The seller's own words.

    Returns:
        An :class:`IntakeResult` naming the product created.

    Raises:
        IntakeError: With a reason that can be shown to the seller verbatim.
    """
    already = db.scalar(select(ParsedMedia).where(ParsedMedia.provider_media_id == media_id))
    draft = parse_once(db, media_id, caption, media_url)

    if not draft.is_product:
        raise IntakeError("That doesn't look like something you're selling.")

    product = create_from_draft(db, seller, draft)
    return IntakeResult(product=product, draft=draft, cached=already is not None)
