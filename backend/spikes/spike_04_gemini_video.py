"""
SPIKE 04 — TIER 3. Can Gemini hear a price spoken in the clip?

    python spikes/spike_04_gemini_video.py [n]

THE decisive spike for this project. For @zumamitumbabales:

  tier 1 (caption)     0/10 — pure hashtag soup, no prices  (spike 01)
  tier 2 (cover image) 0/4  — covers carry no printed price (spike 03)
  tier 3 (video)       ???  — this spike

If tier 3 fails too, this seller cannot be catalogued automatically at all and
the manual-upload path becomes their only route. That is a product finding, not
a bug — and better learned now than after building three services on the
assumption it works.

COSTS TWICE: one Apify run with `shouldDownloadVideos` enabled (the earlier
spikes disabled it, so no video URL was returned), plus one Gemini video call
per clip. `n` is small on purpose.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"
VIDEO_DIR = OUT_DIR / "video"

#: Gemini accepts inline bytes below ~20 MB; larger needs the Files API.
INLINE_LIMIT_BYTES = 18 * 1024 * 1024


class VideoDraft(BaseModel):
    """What the model must return after watching AND listening to one clip."""

    is_product: bool = Field(description="Is a sellable item being shown?")
    name: str = Field(description="Short product name in plain English.")
    description: str = Field(description="One business-like sentence for a buyer.")
    price_kes: int | None = Field(
        default=None,
        description="Price in whole KES if SPOKEN or SHOWN. Null if never "
        "stated. Never guess from appearance.",
    )
    price_heard_as: str | None = Field(
        default=None,
        description="The exact words the price was stated in, e.g. 'mia tano' "
        "or 'five hundred bob'. Null if no price.",
    )
    sizes: list[str] = Field(default_factory=list)
    spoken_language: str | None = Field(
        default=None, description="Language heard: English, Swahili, Sheng, or mixed."
    )
    confidence: float = Field(description="0-1 confidence in the price specifically.")


PROMPT = """You are drafting a product listing for a Kenyan seller's online shop,
from one of their TikTok videos.

WATCH the video and LISTEN to the audio.

Kenyan sellers usually SAY the price out loud rather than writing it. It may be
in English, Swahili, or Sheng:
  "mia tano"      = 500
  "elfu moja"     = 1000
  "one five"      = 1500
  "five hundred bob" = 500
  "kumi"          = 10  (or 10,000 in bale trading context — use judgement)

Rules:
- Report a price ONLY if it is spoken aloud or clearly shown on screen.
- If no price is stated, return null. NEVER estimate from appearance. A wrong
  price is far worse than a missing one.
- Phone numbers are contacts, never prices.
- Record the exact words in price_heard_as so a human can verify your reading.
- Write name and description in plain English even if the audio is Sheng.

Caption hashtags (context only): {hashtags}
"""


def fetch_with_videos(handle: str, limit: int) -> list[dict[str, Any]]:
    """Re-run the actor WITH video download so we get playable media URLs."""
    token = settings.require("apify_token")
    actor = settings.apify_tiktok_actor_id

    print(f"Apify: re-running for @{handle} with video download ({limit} posts)...")
    response = httpx.post(
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
        params={"token": token},
        json={
            "profiles": [handle],
            "resultsPerPage": limit,
            "shouldDownloadVideos": True,
            "shouldDownloadCovers": False,
            "shouldDownloadSubtitles": False,
        },
        timeout=600.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Apify {response.status_code}: {response.text[:800]}")
    items: list[dict[str, Any]] = response.json()

    path = OUT_DIR / f"apify_{handle}_with_video.json"
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved: {path}")
    return items


def main() -> int:
    handle = "zumamitumbabales"
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    cached = OUT_DIR / f"apify_{handle}_with_video.json"
    if cached.exists():
        print(f"Reusing cached payload: {cached}")
        items = json.loads(cached.read_text(encoding="utf-8"))
    else:
        items = fetch_with_videos(handle, limit)

    items = items[:limit]
    client = genai.Client(api_key=settings.require("gemini_api_key"))
    model = settings.gemini_model
    print(f"\nModel: {model}   ·   {len(items)} clips\n")

    results: list[dict[str, Any]] = []
    priced = 0

    for i, item in enumerate(items):
        media = item.get("mediaUrls") or []
        if not media:
            print(f"[{i}] no mediaUrls — actor returned no downloadable video")
            continue

        duration = (item.get("videoMeta") or {}).get("duration")
        print(f"[{i}] downloading clip ({duration}s)...")

        # Apify stores downloaded videos in its own key-value store, which is
        # NOT public — the URL 403s without the API token. Discovered the hard
        # way: the first run silently fetched a 214-byte JSON error and posted
        # it to Gemini as "video".
        params = {"token": settings.require("apify_token")} if "api.apify.com" in media[0] else None
        try:
            resp = httpx.get(media[0], params=params, timeout=180.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            print(f"     download failed: {exc}")
            continue

        video_bytes = resp.content
        content_type = resp.headers.get("content-type", "")

        # Refuse to spend a paid Gemini call on something that is not a video.
        # A silent failure here costs money AND produces a confusing result.
        if resp.status_code >= 400 or "video" not in content_type:
            print(f"     NOT A VIDEO — status {resp.status_code}, type {content_type!r}")
            print(f"     body: {resp.text[:200]}")
            continue
        if len(video_bytes) < 10_000:
            print(f"     suspiciously small ({len(video_bytes)} bytes) — skipping")
            continue

        size_mb = len(video_bytes) / 1024 / 1024
        print(f"     {size_mb:.1f} MB  ({content_type})")
        if len(video_bytes) > INLINE_LIMIT_BYTES:
            print("     too large for inline bytes — the Files API is needed here")
            continue

        (VIDEO_DIR / f"{item.get('id')}.mp4").write_bytes(video_bytes)

        tags = [t.get("name", "") for t in (item.get("hashtags") or []) if isinstance(t, dict)]

        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=video_bytes, mime_type="video/mp4"),
                PROMPT.format(hashtags=", ".join(tags) or "none"),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VideoDraft,
                temperature=0.1,
            ),
        )

        draft = VideoDraft.model_validate_json(response.text or "{}")
        results.append({"video_id": item.get("id"), **draft.model_dump()})
        if draft.price_kes:
            priced += 1

        print(f"     name       : {draft.name}")
        print(
            f"     price      : {f'KES {draft.price_kes:,}' if draft.price_kes else '— not stated'}"
        )
        print(f"     heard as   : {draft.price_heard_as or '—'}")
        print(f"     language   : {draft.spoken_language or '—'}")
        print(f"     sizes      : {draft.sizes or '—'}")
        print(f"     confidence : {draft.confidence:.2f}")
        print()

    print("=" * 70)
    print(f"TIER 3 YIELD: {priced}/{len(results)} clips had a stated price")
    print("=" * 70)
    if results and priced:
        print("The cascade works. Tier 3 recovers what tiers 1 and 2 could not —")
        print("which is the entire reason it exists.")
    elif results:
        print("Tier 3 found nothing either. For THIS seller the catalogue cannot")
        print("be priced automatically; manual entry is their route. A real")
        print("product finding, and cheap to have learned now.")

    out = OUT_DIR / f"gemini_video_{handle}.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
