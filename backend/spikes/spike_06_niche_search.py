"""
SPIKE 06 — can we FIND competitors, not just read a handle we were given?

    python spikes/spike_06_niche_search.py            # schema only, FREE
    python spikes/spike_06_niche_search.py --run      # runs a real search, PAID

Spike 05 proved we can already pull views, comments and followers for an account
whose handle we know. The growth engine needs something harder:

    "show me creators in MY category, and how they are performing"

That is discovery, not lookup, and nothing so far proves the actor can do it.
Everything in BiasharaIntel — benchmarking, outliers, the hook corpus — rests on
this one capability.

STAGE 1 (free): read the actor's input schema and report which discovery modes
it accepts. No run, no charge.

STAGE 2 (paid, --run): actually search, and measure what comes back and what it
cost. The cost number is what every quota and price decision downstream needs.
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

#: Input fields that would let us DISCOVER creators rather than look one up.
DISCOVERY_FIELDS = (
    "searchQueries",
    "hashtags",
    "searchSection",
    "keyword",
    "keywords",
    "profiles",
    "postURLs",
)

#: Small on purpose. This is a probe, and every post is billable.
SEARCH_RESULTS = 10


def fetch_input_schema(token: str, actor: str) -> dict[str, Any]:
    """
    Read the actor's declared input schema. Free — metadata, not a run.

    Raises:
        RuntimeError: If Apify will not describe the actor, with its own message.
    """
    response = httpx.get(
        f"https://api.apify.com/v2/acts/{actor}/builds/default",
        params={"token": token},
        timeout=60.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Apify returned {response.status_code}: {response.text[:400]}")

    payload = response.json().get("data", {})
    schema_raw = payload.get("inputSchema")
    if isinstance(schema_raw, str):
        parsed: dict[str, Any] = json.loads(schema_raw)
        return parsed
    return schema_raw or {}


def report_schema(schema: dict[str, Any]) -> list[str]:
    """Print which discovery modes exist, and return the ones found."""
    properties: dict[str, Any] = schema.get("properties", {})

    print("=" * 68)
    print("DISCOVERY MODES THIS ACTOR ACCEPTS")
    print("=" * 68)

    found: list[str] = []
    for field in DISCOVERY_FIELDS:
        spec = properties.get(field)
        if spec is None:
            print(f"  [ NO ] {field}")
            continue
        found.append(field)
        title = spec.get("title", "")
        print(f"  [ YES] {field:<16} {title}")
        description = (spec.get("description") or "").replace("\n", " ")
        if description:
            print(f"         {description[:110]}")

    print(f"\n  actor declares {len(properties)} input fields in total")
    return found


def run_search(token: str, actor: str, term: str) -> list[dict[str, Any]]:
    """
    Run one real search. PAID.

    Args:
        term: A niche keyword or hashtag, e.g. "mitumba".

    Returns:
        Raw dataset items.
    """
    print(f"\nRunning a real search for '{term}' (max {SEARCH_RESULTS} results)...")

    response = httpx.post(
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
        params={"token": token},
        json={
            "searchQueries": [term],
            "resultsPerPage": SEARCH_RESULTS,
            "shouldDownloadCovers": False,
            "shouldDownloadVideos": False,
            "shouldDownloadSubtitles": False,
        },
        timeout=600.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Apify returned {response.status_code}: {response.text[:600]}")

    items: list[dict[str, Any]] = response.json()
    return items


def report_results(items: list[dict[str, Any]], term: str) -> None:
    """Report what a search returns, from a competitor-benchmarking point of view."""
    print("=" * 68)
    print(f"WHAT '{term}' RETURNED")
    print("=" * 68)
    print(f"  {len(items)} items\n")

    if not items:
        print("  Nothing came back. This input mode may not be supported.")
        return

    # The question that matters: does a search result carry enough to BENCHMARK
    # a creator, or only to see one post?
    creators: dict[str, dict[str, Any]] = {}
    for item in items:
        author = item.get("authorMeta") or {}
        handle = author.get("name")
        if not handle:
            continue
        entry = creators.setdefault(
            handle,
            {"followers": author.get("fans"), "posts": 0, "views": 0, "comments": 0},
        )
        entry["posts"] += 1
        entry["views"] += item.get("playCount") or 0
        entry["comments"] += item.get("commentCount") or 0

    print(f"  distinct creators discovered: {len(creators)}\n")
    print(f"  {'handle':<26} {'followers':>10} {'posts':>6} {'views':>9} {'comments':>9}")
    print(f"  {'-' * 26} {'-' * 10} {'-' * 6} {'-' * 9} {'-' * 9}")
    for handle, data in sorted(creators.items(), key=lambda kv: kv[1]["views"], reverse=True):
        followers = data["followers"]
        print(
            f"  @{handle:<25} {followers if followers is not None else '?':>10} "
            f"{data['posts']:>6} {data['views']:>9} {data['comments']:>9}"
        )

    print("\n  VERDICT")
    have_followers = sum(1 for d in creators.values() if d["followers"] is not None)
    if have_followers == len(creators):
        print("    Follower counts came back for every creator — benchmarking works")
        print("    from a single search, with no second call per creator.")
    elif have_followers:
        print(f"    Followers present for only {have_followers}/{len(creators)}.")
        print("    Benchmarking may need a second lookup per creator — which")
        print("    multiplies the cost, and changes the quota maths.")
    else:
        print("    No follower counts. Benchmarking would need a profile fetch")
        print("    per creator — significantly more expensive per search.")


def main() -> int:
    token = settings.require("apify_token")
    actor = settings.apify_tiktok_actor_id
    do_run = "--run" in sys.argv
    term = next((a for a in sys.argv[1:] if not a.startswith("--")), "mitumba")

    print(f"actor: {actor}\n")

    try:
        schema = fetch_input_schema(token, actor)
    except (RuntimeError, ValueError) as exc:
        print(f"Could not read the input schema: {exc}", file=sys.stderr)
        return 1

    found = report_schema(schema)

    if not do_run:
        print("\n" + "=" * 68)
        print("Schema only — nothing was charged.")
        print("Re-run with --run to perform a real search and measure the cost.")
        print("=" * 68)
        return 0

    if "searchQueries" not in found:
        print("\nThis actor does not accept searchQueries; a different actor is needed.")
        return 1

    items = run_search(token, actor, term)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"apify_search_{term}.json"
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved: {path}\n")

    report_results(items, term)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
