"""
SPIKE 03 — can Gemini read a product and price off a REAL cover image?

    python spikes/spike_03_gemini_cover.py [n]

This is the spike that validates the product's core claim. Captions carry no
prices (spike 01: 0/10 for this seller), so the entire catalogue depends on the
model reading a price from the media instead. If tier 2 works on real covers,
the cascade holds. If it does not, tier 3 (video) has to carry everything and
the cost model changes.

Uses the covers already saved by spike 01, so no Apify spend. Gemini calls DO
cost — `n` is capped low on purpose.

STRUCTURED OUTPUT, not parsed hope. The model is handed a schema and generation
is constrained to it, so we get a validated object rather than a string to
gamble on. This mirrors what production will do.
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

#: How many covers to send. Each is a paid call — keep this small.
DEFAULT_SAMPLE = 4


class ProductDraft(BaseModel):
    """
    What the model must return for one cover image.

    Deliberately small. Every extra field is another thing it can get subtly
    wrong, and everything here is correctable by the seller in review.

    Note what is ABSENT: no stock, no contact details. The agent proposes a
    draft; it never touches inventory or anything that could reach a buyer
    unattended.
    """

    is_product: bool = Field(
        description="False for anything that is not a sellable item — a face, "
        "a shop interior, a text-only announcement."
    )
    name: str = Field(description="Short product name, e.g. 'Zara Sandals'. Not a caption.")
    description: str = Field(description="One business-like sentence a buyer would find useful.")
    price_kes: int | None = Field(
        default=None,
        description="Price in whole Kenyan shillings, ONLY if it is clearly "
        "printed in the image. Null if not visible. Never guess.",
    )
    sizes: list[str] = Field(
        default_factory=list,
        description="Sizes printed in the image, verbatim, e.g. ['37','38','39'].",
    )
    confidence: float = Field(description="0-1, your own confidence in this reading.")


PROMPT = """You are drafting a product listing for a Kenyan seller's online shop.

You are looking at the cover image of one of their TikTok videos.

Rules:
- The price is often printed ON the image, sometimes in Sheng or Swahili
  (e.g. "bei 1500", "1500 only", "@1500"). Read it if it is there.
- If no price is visibly printed, return null. NEVER guess or infer a price
  from what the item looks like it should cost — a wrong price is far worse
  than a missing one.
- Ignore phone numbers. A number like 0712345678 or 0105515839 is a contact,
  never a price.
- Write the name and description in plain English, even when the image text is
  in Sheng or Swahili.
- If the image is not a sellable product, set is_product to false.

Hashtags from the post (context only, they never contain the price): {hashtags}
"""


def load_items(handle: str) -> list[dict[str, Any]]:
    path = OUT_DIR / f"apify_{handle}_raw.json"
    if not path.exists():
        print(f"No saved payload at {path}. Run spike 01 first.", file=sys.stderr)
        raise SystemExit(1)
    items: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return items


def main() -> int:
    handle = "zumamitumbabales"
    sample = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SAMPLE

    items = load_items(handle)[:sample]
    client = genai.Client(api_key=settings.require("gemini_api_key"))
    model = settings.gemini_model

    print(f"Model: {model}   ·   {len(items)} covers   ·   @{handle}\n")

    results: list[dict[str, Any]] = []
    priced = 0

    for i, item in enumerate(items):
        cover_url = (item.get("videoMeta") or {}).get("coverUrl")
        if not cover_url:
            print(f"[{i}] no coverUrl, skipped")
            continue

        tags = [t.get("name", "") for t in (item.get("hashtags") or []) if isinstance(t, dict)]

        try:
            image_bytes = httpx.get(cover_url, timeout=30.0, follow_redirects=True).content
        except httpx.HTTPError as exc:
            print(f"[{i}] cover download failed: {exc}")
            continue

        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                PROMPT.format(hashtags=", ".join(tags) or "none"),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ProductDraft,
                temperature=0.1,  # extraction, not creativity
            ),
        )

        draft = ProductDraft.model_validate_json(response.text or "{}")
        results.append({"video_id": item.get("id"), **draft.model_dump()})
        if draft.price_kes:
            priced += 1

        price = f"KES {draft.price_kes:,}" if draft.price_kes else "— not printed"
        flag = "" if draft.is_product else "   [NOT A PRODUCT]"
        print(f"[{i}] {draft.name}{flag}")
        print(f"     price      : {price}")
        print(f"     sizes      : {draft.sizes or '—'}")
        print(f"     confidence : {draft.confidence:.2f}")
        print(f"     desc       : {draft.description[:80]}")
        print()

    print("=" * 70)
    print(f"TIER 2 YIELD: {priced}/{len(results)} covers had a readable price")
    print("=" * 70)
    if priced == 0:
        print("Tier 2 found nothing. Tier 3 (video, audio + visual) must carry")
        print("this seller — which is exactly why the cascade has a third tier.")
    elif priced < len(results):
        print("Partial yield — the cascade escalates only the ones that missed,")
        print("which is the whole point of ordering tiers by cost.")
    else:
        print("Every cover carried a price. Tier 3 would not be needed here.")

    out = OUT_DIR / f"gemini_cover_{handle}.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
