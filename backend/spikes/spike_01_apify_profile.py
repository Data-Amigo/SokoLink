"""
SPIKE 01 — what does the Apify TikTok actor actually return for a real seller?

    python spikes/spike_01_apify_profile.py zumamitumbabales

WHY a spike before any schema or service code: the fields we think exist and the
fields that exist are different sets, and guessing produces a data model that
has to be migrated the first time real data arrives. TIKTOK's spike 00 killed
the assumption that captions carry prices — that one probe saved the entire
caption-parsing design.

THIS COSTS MONEY. Apify bills roughly $0.30 per 1,000 posts. `resultsPerPage` is
capped low on purpose, and the raw response is written to `spikes/out/` so every
subsequent question about the payload can be answered offline, for free.

Throwaway by design. Nothing in app/ imports this.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

OUT_DIR = Path(__file__).parent / "out"

#: Deliberately small. This is a probe, not an import.
RESULTS_PER_PAGE = 10

#: Apify runs the actor and returns dataset items in one call. Generous timeout:
#: a cold actor can take a minute or more to boot before it scrapes anything.
TIMEOUT_SECONDS = 300.0


def fetch_profile(handle: str) -> list[dict[str, Any]]:
    """
    Run the actor against one profile and return its dataset items.

    Args:
        handle: TikTok handle without the leading @.

    Returns:
        Raw dataset items, exactly as Apify returned them — deliberately
        unparsed, because the point of the spike is to see the real shape.

    Raises:
        RuntimeError: If Apify returns a non-2xx response, with the body
            included; a bare status code is not enough to debug an actor.
    """
    token = settings.require("apify_token")
    actor = settings.apify_tiktok_actor_id

    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"

    payload = {
        "profiles": [handle],
        "resultsPerPage": RESULTS_PER_PAGE,
        # We store our own copies of covers later; the actor does not need to.
        "shouldDownloadCovers": False,
        "shouldDownloadVideos": False,
        "shouldDownloadSubtitles": False,
        "shouldDownloadSlideshowImages": False,
    }

    print(f"Running {actor} for @{handle} (max {RESULTS_PER_PAGE} posts)...")

    response = httpx.post(
        url,
        params={"token": token},
        json=payload,
        timeout=TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        raise RuntimeError(f"Apify returned {response.status_code}: {response.text[:1000]}")

    items: list[dict[str, Any]] = response.json()
    return items


def summarise(items: list[dict[str, Any]]) -> None:
    """Print the findings that actually drive schema decisions."""
    print(f"\n{'=' * 70}")
    print(f"{len(items)} items returned")
    print("=" * 70)

    if not items:
        print("Nothing came back. Private profile, wrong handle, or actor change.")
        return

    first = items[0]
    print(f"\nTOP-LEVEL KEYS ({len(first)}):")
    for key in sorted(first):
        value = first[key]
        kind = type(value).__name__
        preview = ""
        if isinstance(value, str):
            preview = f" — {value[:60]!r}" + ("..." if len(value) > 60 else "")
        elif isinstance(value, (int, float, bool)):
            preview = f" — {value}"
        elif isinstance(value, list):
            preview = f" — {len(value)} items"
        elif isinstance(value, dict):
            preview = f" — keys: {sorted(value)[:6]}"
        print(f"  {key:<28} {kind:<10}{preview}")

    # ── The question that decides the whole parsing design ────────────────────
    print(f"\n{'=' * 70}")
    print("DO CAPTIONS CARRY PRICES?")
    print("=" * 70)

    import re

    price_words = re.compile(r"(?i)\b(ksh|kes|bob|shilling|price|bei|pesa)\b")
    # 3+ digit runs that are not obviously a phone number.
    price_number = re.compile(r"\b\d{3,6}\b")
    phone_like = re.compile(r"(?:\+?254|0)[17]\d{8}")

    with_text = 0
    mention_currency = 0
    have_number = 0

    for item in items:
        caption = item.get("text") or ""
        if caption.strip():
            with_text += 1
        if price_words.search(caption):
            mention_currency += 1
        stripped = phone_like.sub("", caption)
        if price_number.search(stripped):
            have_number += 1

    print(f"  captions with any text        : {with_text}/{len(items)}")
    print(f"  mention a currency/price word : {mention_currency}/{len(items)}")
    print(f"  contain a price-like number   : {have_number}/{len(items)}")

    print("\nCAPTIONS (first 5, truncated):")
    for i, item in enumerate(items[:5]):
        caption = (item.get("text") or "").replace("\n", " ")
        print(f"  [{i}] {caption[:110]}")

    # ── Fields the Product model depends on ──────────────────────────────────
    print(f"\n{'=' * 70}")
    print("FIELDS OUR MODEL NEEDS")
    print("=" * 70)

    wanted = {
        "id": "tiktok_video_id",
        "text": "raw_caption",
        "webVideoUrl": "video_url",
        "playCount": "views",
        "diggCount": "likes",
        "commentCount": "comments",
        "shareCount": "shares",
        "hashtags": "hashtags",
        "videoMeta": "cover_url (nested)",
        "authorMeta": "seller profile (nested)",
    }
    for key, maps_to in wanted.items():
        present = sum(1 for it in items if it.get(key) not in (None, "", [], {}))
        mark = "OK  " if present == len(items) else "PART" if present else "MISS"
        print(f"  [{mark}] {key:<16} -> {maps_to:<26} present in {present}/{len(items)}")

    if "videoMeta" in first and isinstance(first["videoMeta"], dict):
        print(f"\n  videoMeta keys: {sorted(first['videoMeta'])}")
    if "authorMeta" in first and isinstance(first["authorMeta"], dict):
        print(f"  authorMeta keys: {sorted(first['authorMeta'])}")


def main() -> int:
    handle = sys.argv[1] if len(sys.argv) > 1 else "zumamitumbabales"
    handle = handle.lstrip("@").lower()

    items = fetch_profile(handle)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = OUT_DIR / f"apify_{handle}_raw.json"
    raw_path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Raw payload saved: {raw_path}")

    summarise(items)

    print(
        "\nEvery further question about this payload can now be answered from "
        "that file, offline and for free."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
