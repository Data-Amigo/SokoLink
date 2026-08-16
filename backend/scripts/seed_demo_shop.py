"""
Seed a real, browsable demo shop.

    python scripts/seed_demo_shop.py

    live scrape (or saved spike data) ──> Seller + SocialAccount + Products
                                                        │
                                          covers stored locally
                                                        │
                                            http://localhost:8000/<slug>

WHY IT SCRAPES RATHER THAN REPLAYING SAVED DATA: TikTok cover URLs are signed
and expire within days. The first version of this script replayed spike 01's
saved payload and produced a shop of ten broken images — the exact failure
spike 02 had predicted.

Refreshing the saved posts does not work either: an active seller has posted
since, so their old videos have dropped out of the recent feed and there is
nothing to refresh them from. So we take what is live now, and store our own
copy of every cover.

Costs one small Apify run (~12 posts). Falls back to saved data when no token is
configured — with broken images, which is honest rather than mysterious.

AI drafts from spikes 03 and 04 are applied wherever a post id still matches, so
the real prices the model heard survive when they can.

Idempotent. Leaves every other seller alone.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    IngestMethod,
    Platform,
    PriceSource,
    Product,
    ProductStatus,
    Seller,
    SocialAccount,
    VerificationMethod,
)
from app.schemas.tiktok import ScrapedProfile, TikTokAuthor, TikTokVideo  # noqa: E402
from app.services.accounts import create_account  # noqa: E402
from app.services.media import store_cover  # noqa: E402
from app.services.scraper import ApifyEngine, ScraperError  # noqa: E402

SPIKE_DIR = Path(__file__).resolve().parents[1] / "spikes" / "out"

HANDLE = "zumamitumbabales"
DEMO_SLUG = "zuma-mitumba-bales"
DEMO_EMAIL = "demo@biasharamall.com"
DEMO_PASSWORD = "demo-shop-password"

#: The bale price spike 04 heard on every clip: "@3000 30pairs". This seller
#: quotes the same lot price across their feed, so it is a fair default for
#: posts the video tier never saw — and it is what they would enter in review.
BALE_PRICE_KES = 3000
BALE_QUANTITY = 30
BALE_UNIT = "pairs"

#: Taken from the seller's own bio, which really does carry it — spike 02 found
#: three numbers in this profile.
DEMO_WHATSAPP = "254105515839"


def load(name: str) -> Any:
    path = SPIKE_DIR / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def get_profile() -> ScrapedProfile | None:
    """
    A feed with live cover URLs, or the saved payload as a fallback.

    Returns:
        The profile to seed from, or None if there is neither.
    """
    if settings.apify_token:
        print("  scraping the current feed (small Apify run)...")
        try:
            profile = ApifyEngine().fetch_profile(HANDLE, limit=12)
            print(f"  got {profile.video_count} live posts")
            return profile
        except ScraperError as exc:
            print(f"  scrape failed: {exc}")

    raw = load(f"apify_{HANDLE}_raw.json")
    if raw is None:
        return None

    print("  using saved spike data — cover URLs have almost certainly expired")
    return ScrapedProfile(
        author=TikTokAuthor.model_validate(raw[0].get("authorMeta") or {}),
        videos=[TikTokVideo.from_apify(item) for item in raw],
    )


def main() -> int:
    profile = get_profile()
    if profile is None:
        print(
            f"No data. Set APIFY_TOKEN, or run spikes/spike_01_apify_profile.py "
            f"to populate {SPIKE_DIR}.",
            file=sys.stderr,
        )
        return 1

    # What the vision model actually returned, where those posts still exist.
    cover_drafts = {d["video_id"]: d for d in (load(f"gemini_cover_{HANDLE}.json") or [])}
    video_drafts = {d["video_id"]: d for d in (load(f"gemini_video_{HANDLE}.json") or [])}

    scraper = ApifyEngine() if settings.apify_token else None
    author = profile.author

    with SessionLocal() as db:
        seller = db.scalar(select(Seller).where(Seller.slug == DEMO_SLUG))

        if seller is None:
            account = create_account(
                db,
                email=DEMO_EMAIL,
                password=DEMO_PASSWORD,
                shop_name="Zuma Mitumba Bales",
                full_name="Demo Seller",
            )
            seller = account.seller
            assert seller is not None
            seller.slug = DEMO_SLUG
            print(f"  created login {DEMO_EMAIL} / {DEMO_PASSWORD}")
        else:
            print(f"  reusing shop /{DEMO_SLUG}")

        seller.display_name = author.display_name or "ZUMA MITUMBA BALES"
        seller.bio = author.bio
        seller.avatar_url = author.avatar_url
        seller.location = "Nairobi, Kenya"
        seller.whatsapp_number = DEMO_WHATSAPP
        seller.is_published = True

        social = seller.account_for(Platform.TIKTOK)
        if social is None:
            social = SocialAccount(
                seller_id=seller.id,
                platform=Platform.TIKTOK.value,
                handle=author.handle,
                # Demo data, verified as if by bio code — which is how this
                # seller would really have connected.
                verified_at=datetime.now(UTC),
                verification_method=VerificationMethod.BIO_CODE.value,
            )
            db.add(social)
            db.flush()

        social.display_name = author.display_name
        social.avatar_url = author.avatar_url
        social.bio = author.bio
        social.follower_count = author.follower_count
        social.post_count = author.video_count
        social.last_synced_at = datetime.now(UTC)

        existing = {
            p.platform_post_id: p
            for p in db.scalars(select(Product).where(Product.seller_id == seller.id)).all()
            if p.platform_post_id
        }

        created = 0
        with_stored_cover = 0
        priced_by_ai = 0

        for video in profile.videos:
            product = existing.get(video.video_id)
            if product is None:
                product = Product(
                    seller_id=seller.id,
                    social_account_id=social.id,
                    platform=Platform.TIKTOK.value,
                    ingest_method=IngestMethod.PROFILE_SYNC.value,
                    platform_post_id=video.video_id,
                    title="Mixed Ladies Sandals",
                )
                db.add(product)
                created += 1

            product.source_url = video.video_url
            product.raw_caption = video.caption
            product.hashtags = video.hashtags
            product.views = video.views
            product.likes = video.likes
            product.comments = video.comments
            product.shares = video.shares

            # Our own copy — the platform's URL expires, ours does not.
            if video.cover_url and scraper is not None:
                stored = store_cover(
                    scraper,
                    remote_url=video.cover_url,
                    platform=Platform.TIKTOK.value,
                    post_id=video.video_id,
                )
                product.cover_url = stored or video.cover_url
                if stored:
                    with_stored_cover += 1
            elif video.cover_url:
                product.cover_url = video.cover_url

            # Prefer what the model HEARD over what it saw: spike 04 found
            # prices on 3/3 clips where spike 03 found none on the covers.
            heard = video_drafts.get(video.video_id)
            seen = cover_drafts.get(video.video_id)
            best = heard or seen

            if best:
                product.title = best.get("name") or product.title
                product.description = best.get("description")
                product.parse_confidence = best.get("confidence")
                product.sizes = best.get("sizes") or []

            if heard and heard.get("price_kes"):
                product.price_kes = heard["price_kes"]
                product.price_evidence = heard.get("price_heard_as")
                product.price_source = PriceSource.VIDEO.value
                product.unit_quantity = BALE_QUANTITY
                product.unit_label = BALE_UNIT
                priced_by_ai += 1
            elif product.price_kes is None:
                product.price_kes = BALE_PRICE_KES
                product.unit_quantity = BALE_QUANTITY
                product.unit_label = BALE_UNIT
                product.price_source = PriceSource.SELLER.value

            # Published as a seller would after reviewing — a demo of nothing
            # but drafts shows an empty storefront.
            product.status = ProductStatus.PUBLISHED.value
            product.reviewed_at = datetime.now(UTC)
            product.stock = 5

        db.commit()

        total = len(profile.videos)
        print(f"\n  seller        /{seller.slug}  ({seller.display_name})")
        print(f"  connected     @{social.handle}  ·  {social.follower_count:,} followers")
        print(f"  products      {total} ({created} new)")
        print(f"  covers stored {with_stored_cover}/{total}")
        print(f"  AI-read price {priced_by_ai}/{total}")
        print(f"\n  Storefront:   http://localhost:8000/{seller.slug}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
