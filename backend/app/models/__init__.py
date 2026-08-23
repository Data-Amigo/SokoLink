"""SQLAlchemy models — the database rails.

Constraints defined here are enforced by Postgres itself, so no code path — a
service, a migration script, a future agent, or a hand-written query — can
bypass them. The ones that carry the most weight:

    published_requires_price   the AI can never push an unpriced item live
    stock_non_negative         overselling is impossible at the storage layer
    uq_products_platform_post  re-syncing a feed updates, never duplicates
    upload_has_no_post_id      an uploaded product cannot look re-syncable
    published_needs_whatsapp   a live shop always has a way to be contacted

IMPORTANT: every model module must be imported here. Alembic autogenerate
compares the database against ``Base.metadata``; a model that is never imported
is invisible to it, producing a migration that silently drops what it cannot
see.
"""

from app.db import Base
from app.models.account import Account
from app.models.account_claim import AccountClaim
from app.models.cart import Cart, CartItem
from app.models.enums import (
    IngestMethod,
    JobStatus,
    OrderStatus,
    PaymentMethodKind,
    Platform,
    PriceSource,
    ProductStatus,
    ScrapeStatus,
    VerificationMethod,
)
from app.models.job import Job
from app.models.login_code import LoginCode
from app.models.order import Order, OrderItem, Payment
from app.models.payment_method import PaymentMethod
from app.models.post import Post
from app.models.product import Product
from app.models.scrape_job import ScrapeJob
from app.models.seller import Seller
from app.models.snapshot import AccountMetricSnapshot, PostMetricSnapshot
from app.models.social_account import SocialAccount

__all__ = [
    "Account",
    "AccountClaim",
    "AccountMetricSnapshot",
    "Base",
    "Cart",
    "CartItem",
    "IngestMethod",
    "Job",
    "JobStatus",
    "LoginCode",
    "Order",
    "OrderItem",
    "OrderStatus",
    "Payment",
    "PaymentMethod",
    "PaymentMethodKind",
    "Platform",
    "Post",
    "PostMetricSnapshot",
    "PriceSource",
    "Product",
    "ProductStatus",
    "ScrapeJob",
    "ScrapeStatus",
    "Seller",
    "SocialAccount",
    "VerificationMethod",
]
