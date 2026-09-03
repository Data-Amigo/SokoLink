"""
A forwarded catalogue post becomes a draft product.

    WhatsApp media ──▶ download ──▶ [cache] ──▶ draft agent ──▶ DRAFT product
                          │            │                              ▲
                          │            └── parsed once ever, by media id
                          │                                           │
                          └──▶ media/covers/wa_<hash>.jpg ────────────┘
                               our own copy, because theirs expires

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
their whole catalogue at once, and Meta redelivers anything slow. The cache is
a table rather than a memo so it survives restarts and redeploys.

THE BYTES ARE KEPT, NOT JUST READ. The download happens to show the image to the
model, and for a long time that was all it was used for — every forwarded product
reached the storefront as a card saying "No photo", which threw away the better
half of what the seller sent. The same bytes are now written to our own media
store on the way past. Meta's media URLs are signed and short-lived, so there is
no second chance to fetch them later.

A MISSING PRICE IS A RESULT, NOT A FAILURE. Verified against 24 real captions:
zero mention KSh. Sellers withhold the price deliberately — that is the buyer
bottleneck this product removes — so the common case is a draft that needs one
number from the seller before it can go live.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.draft import DraftAgentError, get_draft_agent
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
from app.services.media import store_image_bytes, stored_image_path

#: How a caller hands over the bytes. Meta's media is an id resolved through
#: two authenticated Graph calls — that belongs to the caller, not here; this
#: module owns the CACHE and the RULES, and the provider is the caller's
#: business.
MediaFetch = Callable[[], bytes]

#: The job kind the worker runs to parse one forwarded photo. Defined here, next
#: to the intake it drives, so the webhook that enqueues it and the handler that
#: runs it name the same string and cannot drift.
PARSE_FORWARD = "parse_forward"


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


def parse_once(
    db: Session, media_id: str, caption: str, fetch: MediaFetch
) -> tuple[ProductDraft, bytes | None]:
    """
    Read one forwarded image, or return what we already paid to learn.

    Args:
        db: Session.
        media_id: The provider's media id — the cache key.
        caption: The seller's own words from the message.
        fetch: Called ONLY on a cache miss, and returns the image bytes.

    Returns:
        The validated draft, and the bytes IF they were downloaded on this
        call. A cache hit returns None for them, because the whole point of the
        cache is that nothing was fetched — the caller falls back to the copy
        stored the first time round.

    Raises:
        IntakeError: When the image cannot be read. A failure is CACHED, so a
            redelivery of a post the model reliably chokes on does not bill us
            again for the same answer.
    """
    seen = db.scalar(select(ParsedMedia).where(ParsedMedia.provider_media_id == media_id))
    if seen is not None:
        if seen.error:
            raise IntakeError(seen.error)
        return ProductDraft.model_validate(seen.draft), None

    # Called here and nowhere earlier: a cache hit must cost neither a download
    # nor an inference, and putting the fetch behind the lookup is what makes
    # "parsed once ever" also mean "downloaded once ever".
    image = fetch()

    try:
        draft = get_draft_agent().draft_from_forwarded(caption, image)
    except DraftAgentError as exc:
        reason = "I couldn't read that one. Try a clearer photo, or type the details."
        db.add(ParsedMedia(provider_media_id=media_id, error=reason))
        # FLUSHED, not merely added. The session runs with autoflush=False, so
        # an un-flushed row is invisible to the SELECT at the top of this
        # function — and this cache exists precisely to be read again moments
        # later, when Meta redelivers the message that just timed out.
        db.flush()
        raise IntakeError(reason) from exc

    db.add(ParsedMedia(provider_media_id=media_id, draft=draft.model_dump(mode="json")))
    db.flush()
    return draft, image


def create_from_draft(
    db: Session, seller: Seller, draft: ProductDraft, *, cover_path: str | None = None
) -> Product:
    """
    Persist one draft as a product the seller can review.

    Args:
        db: Session.
        seller: Whose shop.
        draft: What the agent proposed.
        cover_path: Our stored copy of the forwarded photo, relative to
            MEDIA_ROOT. None leaves the card without a picture rather than
            failing — see the note below.

    Returns:
        The created product, always DRAFT.

    Notes:
        THE PHOTO IS THE POST. A seller forwards a catalogue post because the
        picture is the thing they are selling with; a draft created without it
        renders "No photo" on the storefront and quietly throws away the better
        half of what they sent. It was doing exactly that until the bytes were
        threaded through to here.

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
        cover_url=cover_path,
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
    db: Session, seller: Seller, *, media_id: str, fetch: MediaFetch, caption: str
) -> IntakeResult:
    """
    Turn one forwarded catalogue post into a draft product.

    Args:
        db: Session. The caller commits.
        seller: Whose shop the product joins.
        media_id: The provider's media id, used as the parse cache key.
        fetch: Returns the image bytes. Called only on a cache miss.
        caption: The seller's own words.

    Returns:
        An :class:`IntakeResult` naming the product created.

    Raises:
        IntakeError: With a reason that can be shown to the seller verbatim.
    """
    already = db.scalar(select(ParsedMedia).where(ParsedMedia.provider_media_id == media_id))
    draft, image = parse_once(db, media_id, caption, fetch)

    if not draft.is_product:
        raise IntakeError("That doesn't look like something you're selling.")

    # KEPT, NOT RE-FETCHED. On a cache miss we are holding the bytes the model
    # just read; on a hit we were deliberately not given any, so we look for the
    # copy the first forward stored under the same media id.
    cover = store_image_bytes(image, key=media_id) if image else stored_image_path(media_id)

    product = create_from_draft(db, seller, draft, cover_path=cover)
    return IntakeResult(product=product, draft=draft, cached=already is not None)
