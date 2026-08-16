"""
Storing our own copies of images, because theirs expire.

    TikTok CDN url ──> download ──> media/covers/<platform>_<post_id>.jpg
                                          │
                                    served at /media/...

WHY THIS EXISTS, and it is not a nice-to-have: TikTok cover URLs are SIGNED and
carry an `x-expires` timestamp. Spike 02 flagged it; the demo storefront then
proved it by rendering ten broken images a week after the scrape. A stored copy
is the difference between a shop that works next month and a wall of broken
image icons.

Local disk today. In production the same paths move behind object storage and a
CDN — and because a Product stores a RELATIVE path, nothing else changes when
they do.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.services.scraper import ScraperEngine, ScraperError

#: Where stored media lives. Gitignored — it is regenerable, and large.
MEDIA_ROOT = Path(__file__).resolve().parents[2] / "media"
COVERS_DIR = MEDIA_ROOT / "covers"

#: The URL prefix these are served under. Products store a path relative to
#: MEDIA_ROOT, so moving to object storage changes this constant and nothing else.
MEDIA_URL_PREFIX = "/media"


def cover_filename(platform: str, post_id: str | None, source_url: str) -> str:
    """
    A stable filename for one post's cover.

    Args:
        platform: Which platform the post came from.
        post_id: The platform's post id, when there is one.
        source_url: Falls back to a hash of this for uploads and pasted links
            that carry no post id.

    Returns:
        A filename that is the same every time for the same post, so a re-sync
        overwrites rather than accumulating copies.
    """
    if post_id:
        stem = f"{platform}_{post_id}"
    else:
        digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]
        stem = f"{platform}_{digest}"
    return f"{stem}.jpg"


def store_cover(
    scraper: ScraperEngine,
    *,
    remote_url: str,
    platform: str,
    post_id: str | None,
) -> str | None:
    """
    Download a cover and keep our own copy.

    Args:
        scraper: Used for the download, so the content-type guard and the Apify
            token handling live in one place rather than being reimplemented.
        remote_url: The platform's (expiring) URL.
        platform: For the filename.
        post_id: For the filename.

    Returns:
        A path relative to MEDIA_ROOT, e.g. ``covers/tiktok_7671596.jpg``, or
        None if the download failed.

    Notes:
        Returns None rather than raising, for ANY failure. A missing cover
        degrades one card; letting it escape would abort the sync and lose the
        products too — which is a far worse outcome than a placeholder image.

        The catch is deliberately broad. An earlier version caught only
        ScraperError, which meant a timeout, a full disk or an engine that
        simply had not implemented the method took the whole product down with
        it. A function whose entire contract is "never let an image failure
        cost us a product" cannot afford to enumerate the failures in advance.

        The caller records the failure in its warnings; nothing is swallowed
        silently.
    """
    COVERS_DIR.mkdir(parents=True, exist_ok=True)

    filename = cover_filename(platform, post_id, remote_url)
    destination = COVERS_DIR / filename

    # Already have it. Covers do not change once posted, so re-downloading on
    # every sync would spend bandwidth to overwrite a file with itself.
    if destination.exists() and destination.stat().st_size > 0:
        return f"covers/{filename}"

    try:
        data = scraper.download_media(remote_url, expect="image")
        destination.write_bytes(data)
    except (ScraperError, OSError, NotImplementedError):
        return None

    return f"covers/{filename}"


def public_url(stored_path: str | None) -> str | None:
    """
    Turn a stored relative path into a URL the browser can request.

    Args:
        stored_path: What ``store_cover`` returned, or a full URL for records
            predating local storage.

    Returns:
        A URL, or None.
    """
    if not stored_path:
        return None
    # Anything already absolute is a legacy remote URL — pass it through rather
    # than mangling it into a broken local path.
    if stored_path.startswith(("http://", "https://")):
        return stored_path
    return f"{MEDIA_URL_PREFIX}/{stored_path.lstrip('/')}"
