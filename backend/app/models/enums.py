"""
Shared enumerations.

Stored as strings in Postgres rather than native enum types. Adding a value to a
native PG enum needs a migration and an exclusive lock; adding one here is a
code change. For values that will grow — platforms especially — that difference
matters far more than the few bytes saved.

Validity is enforced by a CHECK constraint on each column, so the database still
refuses a value the application never defined.
"""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    """
    Where a product or an account came from.

    Deliberately generalised before it was needed. The model was TikTok-shaped
    (`tiktok_handle`, `tiktok_video_id`) until 2026-08-08, when multi-channel
    entered the plan. Generalising with zero production rows is a migration
    nobody notices; generalising after fifty sellers have live catalogues means
    backfilling real shops.

    Only TIKTOK has an ingestion engine today. The rest are declared so the
    schema, the URLs and the UI never have to change to admit them.
    """

    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    JUMIA = "jumia"

    #: Not a social platform — the seller uploaded this themselves.
    MANUAL = "manual"

    @property
    def is_social(self) -> bool:
        """Whether this platform can be connected and synced."""
        return self is not Platform.MANUAL

    @property
    def label(self) -> str:
        """Display name, for the dashboard."""
        return {
            Platform.TIKTOK: "TikTok",
            Platform.INSTAGRAM: "Instagram",
            Platform.FACEBOOK: "Facebook",
            Platform.JUMIA: "Jumia",
            Platform.MANUAL: "Uploaded",
        }[self]


class IngestMethod(StrEnum):
    """
    How a product entered the catalogue.

    Separate from :class:`Platform` because they answer different questions.
    Platform says *where it came from*; this says *how it got here* — and it is
    the second that decides what a re-sync may touch.
    """

    PROFILE_SYNC = "profile_sync"
    """Bulk-imported from a connected account. A re-sync owns and may update these."""

    SINGLE_LINK = "single_link"
    """Added from one pasted post URL. Not owned by any sync."""

    UPLOAD = "upload"
    """Uploaded by the seller. A sync must NEVER touch these."""

    @property
    def is_sync_owned(self) -> bool:
        """
        Whether a profile re-sync is allowed to modify or remove this product.

        Only PROFILE_SYNC items. A seller who adds stock by hand, syncs their
        feed, and watches it vanish does not come back — so this is a rail with
        its own test, not a convention.
        """
        return self is IngestMethod.PROFILE_SYNC


class VerificationMethod(StrEnum):
    """
    How a seller proved they control a social account.

    WHY THIS EXISTS AT ALL: without proof, a handle is just a string someone
    typed. A stranger could claim @zumamitumbabales, have us scrape her videos
    and her photos, and publish a storefront pointing at *their* WhatsApp
    number. That is not impersonation, it is sales diversion — and it would be
    invisible to the buyer.

    OAuth is the better experience: one tap, and the platform tells us who they
    are. It is unavailable until TikTok and Meta approve our app, which is on
    their clock. BIO_CODE bridges that gap using only what we already fetch.
    """

    BIO_CODE = "bio_code"
    """Seller placed a one-time code in their profile bio; we read it back."""

    OAUTH = "oauth"
    """The platform itself vouched for them. Preferred, once approved."""


class ProductStatus(StrEnum):
    """
    Where a product sits between arriving and being buyable.

    DRAFT is the default for everything, including AI output. Publishing is a
    deliberate human act that requires a price.
    """

    DRAFT = "draft"
    """Arrived, not yet confirmed by the seller. Invisible to buyers."""

    PUBLISHED = "published"
    """Live on the storefront. Requires a price."""

    ARCHIVED = "archived"
    """Withdrawn by the seller. Kept for order history, hidden from the shop."""


class ScrapeStatus(StrEnum):
    """Lifecycle of one ingestion run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobStatus(StrEnum):
    """
    Lifecycle of one queued unit of background work.

    Deliberately NOT reusing ``ScrapeStatus``: a scrape is one kind of job, and
    sharing the enum would tie the queue's states to ingestion's. When a job
    kind needs a state a scrape does not have, only this changes.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_final(self) -> bool:
        """Whether nothing further will happen to a job in this state."""
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED)


class PriceSource(StrEnum):
    """
    Which tier of the extraction cascade produced the price.

    Recorded per product because it is the feedback signal for the whole
    approach: if the video tier rarely fires, the expensive path is not earning
    its cost. Without this column that question can only be guessed at.
    """

    CAPTION = "caption"
    """Read from caption text. Near-free, and vanishingly rare in practice."""

    COVER_IMAGE = "cover_image"
    """Read from text printed on the cover or an uploaded photo."""

    VIDEO = "video"
    """Heard or seen in the clip itself. Highest yield, highest cost."""

    SELLER = "seller"
    """Typed by a human. Always outranks anything a model produced."""


class PaymentMethodKind(StrEnum):
    """
    How a seller takes money — and therefore how a payment gets confirmed.

    THIS IS NOT A COSMETIC LABEL. It decides the entire checkout path, because
    Safaricom gives us no choice: **Daraja's STK Push and C2B APIs work with
    Paybill and Buy Goods shortcodes only.** Pochi la Biashara is neither, so a
    Pochi seller can never have an automatic confirmation.

    A large share of Kenyan micro-sellers run on Pochi precisely because it
    needs no business registration. So the manual path is permanent and
    first-class, not a shim we delete once STK works — any design treating STK
    as the real path and manual as a fallback is wrong for most of our sellers.
    """

    POCHI = "pochi"
    """Pochi la Biashara, on the seller's personal line. Manual confirmation only."""

    TILL = "till"
    """Buy Goods till. STK-capable, but only if the seller supplies credentials."""

    PAYBILL = "paybill"
    """Paybill shortcode. Same: STK-capable with credentials, manual without."""

    @property
    def supports_stk(self) -> bool:
        """
        Whether Daraja can push a prompt to this kind of destination at all.

        Says nothing about whether it *will* — that also needs the seller to
        have handed over credentials. This is the hard ceiling, not the setting.
        """
        return self in (PaymentMethodKind.TILL, PaymentMethodKind.PAYBILL)


class OrderStatus(StrEnum):
    """
    Where an order sits between placed and paid.

    ``AWAITING_CONFIRMATION`` is the state a boolean could not express, and it
    exists because of the manual path: **somebody says they paid and nobody has
    checked yet.** A buyer-entered M-Pesa code is a claim, not a payment.

        pending ──▶ awaiting_confirmation ──▶ paid
           │              (manual path)         ▲
           │                                    │
           └────────── STK callback ────────────┘
           │
           └──▶ cancelled / failed

    Only the SELLER moves an order into ``PAID`` on the manual path. On the STK
    path the callback does, and the callback is the only automatic truth.
    """

    PENDING = "pending"
    """Placed, nothing paid yet. The STK prompt may still be on the handset."""

    AWAITING_CONFIRMATION = "awaiting_confirmation"
    """A buyer has claimed an M-Pesa code. Unverified until the seller says so."""

    PAID = "paid"
    """Money confirmed — by a callback, or by the seller checking their phone."""

    CANCELLED = "cancelled"
    """Abandoned by the buyer, or withdrawn by the seller."""

    FAILED = "failed"
    """The payment attempt was rejected, timed out, or the buyer declined."""

    @property
    def is_final(self) -> bool:
        """Whether nothing further will happen to an order in this state."""
        return self in (OrderStatus.PAID, OrderStatus.CANCELLED, OrderStatus.FAILED)

    @property
    def is_settled(self) -> bool:
        """Whether the seller should act on it — i.e. money actually arrived."""
        return self is OrderStatus.PAID
