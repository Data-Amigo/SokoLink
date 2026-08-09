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
from app.models.enums import (
    IngestMethod,
    Platform,
    PriceSource,
    ProductStatus,
    ScrapeStatus,
)
from app.models.product import Product
from app.models.scrape_job import ScrapeJob
from app.models.seller import Seller
from app.models.social_account import SocialAccount

__all__ = [
    "Account",
    "Base",
    "IngestMethod",
    "Platform",
    "PriceSource",
    "Product",
    "ProductStatus",
    "ScrapeJob",
    "ScrapeStatus",
    "Seller",
    "SocialAccount",
]
