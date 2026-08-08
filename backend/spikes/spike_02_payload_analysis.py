"""
SPIKE 02 — offline analysis of the payload spike 01 already paid for.

    python spikes/spike_02_payload_analysis.py

FREE. Reads `spikes/out/apify_<handle>_raw.json` from disk and answers the
questions that decide the P1 design. Spend once on the network, analyse many
times — this is why spike 01 writes the raw response to disk.

Questions this answers:
  1. Do TikTok transcriptions exist for this seller? (a cheaper price tier?)
  2. How long are the clips? (drives the cost of the video tier)
  3. Is the cover image usable as tier 2?
  4. What can we auto-fill on the seller profile from authorMeta?
  5. What signal do hashtags actually carry?
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).parent / "out"


def load(handle: str) -> list[dict[str, Any]]:
    path = OUT_DIR / f"apify_{handle}_raw.json"
    if not path.exists():
        print(f"No saved payload at {path}. Run spike 01 first.", file=sys.stderr)
        raise SystemExit(1)
    data: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return data


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def main() -> int:
    handle = (sys.argv[1] if len(sys.argv) > 1 else "zumamitumbabales").lstrip("@").lower()
    items = load(handle)

    # ── 1. Transcriptions: is there a tier cheaper than sending video? ───────
    section("1. TRANSCRIPTIONS — a cheaper price tier than video?")
    have_transcript = 0
    have_subtitles = 0
    for item in items:
        meta = item.get("videoMeta") or {}
        if meta.get("transcriptionLink"):
            have_transcript += 1
        if meta.get("subtitleLinks"):
            have_subtitles += 1
    print(f"  transcriptionLink populated : {have_transcript}/{len(items)}")
    print(f"  subtitleLinks populated     : {have_subtitles}/{len(items)}")
    sample = items[0].get("videoMeta") or {}
    print(f"  sample transcriptionLink    : {sample.get('transcriptionLink')!r}")
    print(f"  sample subtitleLinks        : {sample.get('subtitleLinks')!r}")
    if not have_transcript and not have_subtitles:
        print("\n  -> No transcripts. Tier 3 must send the VIDEO to Gemini.")
        print("     Matches TIKTOK spike 00: no transcriptions for Swahili content.")

    # ── 2. Clip length drives the cost of the video tier ────────────────────
    section("2. CLIP DURATION — cost driver for the video tier")
    durations = [
        (item.get("videoMeta") or {}).get("duration")
        for item in items
        if (item.get("videoMeta") or {}).get("duration") is not None
    ]
    if durations:
        durations_sorted = sorted(d for d in durations if isinstance(d, int | float))
        total = sum(durations_sorted)
        print(f"  clips with a duration : {len(durations_sorted)}/{len(items)}")
        print(f"  shortest / longest    : {durations_sorted[0]}s / {durations_sorted[-1]}s")
        print(f"  mean                  : {total / len(durations_sorted):.1f}s")
        print(f"  total for {len(durations_sorted)} clips     : {total}s")
        print("\n  -> Short clips. Tier 3 is affordable per item, but it is still")
        print("     the most expensive tier and must stay cached once-ever.")

    # ── 3. Cover image — the tier 2 input ───────────────────────────────────
    section("3. COVER IMAGE — the tier 2 input")
    covers = sum(1 for i in items if (i.get("videoMeta") or {}).get("coverUrl"))
    print(f"  coverUrl present : {covers}/{len(items)}")
    cover = (items[0].get("videoMeta") or {}).get("coverUrl", "")
    print(f"  sample host      : {cover.split('/')[2] if '://' in cover else 'n/a'}")
    print(f"  has query params : {'?' in cover}  (signed URL -> expires)")
    print("\n  -> We must download and store covers ourselves. A stored copy is")
    print("     the difference between a storefront that works next month and")
    print("     one full of broken images.")

    # ── 4. Seller profile auto-fill ─────────────────────────────────────────
    section("4. SELLER PROFILE — what we can auto-fill from authorMeta")
    author = items[0].get("authorMeta") or {}
    for key in (
        "name",
        "nickName",
        "fans",
        "heart",
        "video",
        "verified",
        "privateAccount",
        "ttSeller",
        "signature",
        "bioLink",
        "commerceUserInfo",
    ):
        value = author.get(key)
        if isinstance(value, str) and len(value) > 70:
            value = value[:70] + "..."
        print(f"  {key:<18} {value!r}")
    print("\n  -> display_name, follower_count, avatar and bio all auto-fill on")
    print("     connect. The seller confirms rather than types.")

    # ── 5. Hashtags: the only text signal we get ────────────────────────────
    section("5. HASHTAGS — the only text signal in these captions")
    from collections import Counter

    tags: Counter[str] = Counter()
    for item in items:
        for tag in item.get("hashtags") or []:
            name = tag.get("name") if isinstance(tag, dict) else str(tag)
            if name:
                tags[name.lower()] += 1
    print(f"  distinct hashtags across {len(items)} posts: {len(tags)}")
    for name, count in tags.most_common(12):
        print(f"    {count:>2}x  #{name}")
    print("\n  -> Hashtags carry CATEGORY and AUDIENCE, never price. Useful as a")
    print("     hint to the vision model, worthless as a price source.")

    # ── Engagement spread, for Soko Intel outlier detection later ───────────
    section("6. ENGAGEMENT — baseline for outlier detection (Soko Intel)")
    views = sorted(i.get("playCount", 0) for i in items)
    if views:
        mean = sum(views) / len(views)
        print(f"  views: min {views[0]}, max {views[-1]}, mean {mean:.0f}")
        outliers = [v for v in views if v >= mean * 3]
        print(f"  posts at >=3x mean (outlier threshold): {len(outliers)}")
        print("\n  -> With 10 posts the baseline is thin. Outlier detection needs")
        print("     a fuller feed to mean anything.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
