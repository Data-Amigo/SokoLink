"""SQLAlchemy models — the database rails.

Constraints defined here are enforced by Postgres itself, so no code path — a
service, a migration script, a future agent, or a hand-written query — can
bypass them. The three that matter most:

    published_requires_price   the AI can never push an unpriced item live
    stock_non_negative         overselling is impossible at the storage layer
    unique tiktok_video_id     re-scraping a feed updates, never duplicates

IMPORTANT: every model module must be imported here. Alembic autogenerate
compares the database against ``Base.metadata``; a model that is never imported
is invisible to it, producing a migration that silently drops what it cannot
see.
"""

from app.db import Base
from app.models.enums import PriceSource, ProductSource, ProductStatus, ScrapeStatus
from app.models.product import Product
from app.models.scrape_job import ScrapeJob
from app.models.seller import Seller

__all__ = [
    "Base",
    "PriceSource",
    "Product",
    "ProductSource",
    "ProductStatus",
    "ScrapeJob",
    "ScrapeStatus",
    "Seller",
]
