"""
SPIKE 05 — do we already have the metrics the analytics product needs?

    python spikes/spike_05_metric_coverage.py

FREE. Reads the payload spike 01 already paid for and answers, with evidence
rather than memory, three questions the growth engine rests on:

    1. views per post
    2. number of comments per post
    3. followers for the account

If these are present, "My Analytics" needs no new integration — only storage
and a chart. If they are absent, the product needs rethinking before any UI is
drawn.

Deliberately separate from spike 02: that one asked "what shape is this
payload?" This one asks "does it contain the specific numbers a paying creator
expects to see?"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).parent / "out"

#: Apify's field name -> what a creator calls it.
POST_METRICS = {
    "playCount": "views",
    "commentCount": "comments",
    "diggCount": "likes",
    "shareCount": "shares",
    "collectCount": "saves",
}

ACCOUNT_METRICS = {
    "fans": "followers",
    "heart": "total likes",
    "video": "posts",
    "following": "following",
}


def load(handle: str) -> list[dict[str, Any]]:
    path = OUT_DIR / f"apify_{handle}_raw.json"
    if not path.exists():
        print(f"No saved payload at {path}. Run spike 01 first.", file=sys.stderr)
        raise SystemExit(1)
    data: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return data


def main() -> int:
    handle = (sys.argv[1] if len(sys.argv) > 1 else "zumamitumbabales").lstrip("@").lower()
    items = load(handle)

    print(f"@{handle} — {len(items)} posts in the saved payload\n")

    print("=" * 66)
    print("PER-POST METRICS")
    print("=" * 66)
    for field, label in POST_METRICS.items():
        present = sum(1 for it in items if it.get(field) is not None)
        values = [it.get(field, 0) or 0 for it in items]
        mark = "YES" if present == len(items) else "PART" if present else "NO"
        print(
            f"  [{mark:<4}] {label:<8} {present}/{len(items)} posts   "
            f"min {min(values)}  max {max(values)}"
        )

    print("\n  sample of the first five posts:")
    for i, it in enumerate(items[:5]):
        print(
            f"    post {i}:  views {it.get('playCount'):>6}   "
            f"comments {it.get('commentCount'):>4}   "
            f"likes {it.get('diggCount'):>5}   "
            f"shares {it.get('shareCount'):>4}"
        )

    print("\n" + "=" * 66)
    print("ACCOUNT-LEVEL METRICS")
    print("=" * 66)
    author = items[0].get("authorMeta") or {}
    for field, label in ACCOUNT_METRICS.items():
        value = author.get(field)
        mark = "YES" if value is not None else "NO"
        print(f"  [{mark:<4}] {label:<14} {value}")

    print("\n" + "=" * 66)
    print("VERDICT")
    print("=" * 66)
    have_views = all(it.get("playCount") is not None for it in items)
    have_comments = all(it.get("commentCount") is not None for it in items)
    have_followers = author.get("fans") is not None

    for ok, question in (
        (have_views, "views per post"),
        (have_comments, "number of comments per post"),
        (have_followers, "followers for the account"),
    ):
        print(f"  {'CONFIRMED' if ok else 'MISSING  '}  {question}")

    if have_views and have_comments and have_followers:
        print(
            "\n  All three are already in the payload we pay for anyway.\n"
            "  'My Analytics' needs STORAGE and a CHART, not a new integration.\n"
            "  What is NOT proven here: finding a competitor by keyword or\n"
            "  category. That needs a different actor input — spike 06."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
