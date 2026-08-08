"""
The draft agent — media becomes a product draft.

    caption   ──> tier 1  near-free   category hints, almost never a price
    cover     ──> tier 2  cheap       price printed on the image
    video     ──> tier 3  expensive   price SPOKEN aloud, in Sheng

Each tier escalates only when the one above yields no confident price.

MEASURED, not assumed. Against @zumamitumbabales (spikes 01/03/04):

    tier 1   0/10
    tier 2   0/4
    tier 3   3/3     <- the only tier that worked for this seller

Tier 2 stays because it costs a fraction of tier 3 and other sellers do print
prices. Ordering by cost is the entire design.

THIS FILE IS THE ONLY PLACE THAT KNOWS WHICH VISION PROVIDER WE USE. Callers see
``draft_from_cover`` and ``draft_from_video``. Swapping model or provider is a
change here and nowhere else — TIKTOK swung Gemini→OpenAI→Gemini through an
equivalent seam without touching a single caller.

THE AGENT PROPOSES; CODE DISPOSES. Everything returned here is a DRAFT. Nothing
in this module writes to the database, sets stock, or publishes anything.
"""

from __future__ import annotations

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.config import settings
from app.schemas.draft import ProductDraft
from app.schemas.tiktok import TikTokVideo

#: Extraction, not creativity. We want the same answer twice from the same image.
TEMPERATURE = 0.1

_RULES = """
Rules, in order of importance:

1. Report a price ONLY if it is clearly stated — printed on the image, shown on
   screen, or spoken aloud. If it is not stated, return null for price_kes.
   NEVER estimate a price from what the item looks like it should cost. A wrong
   price is far worse than a missing one: the seller can add a missing price in
   seconds, but a wrong one reaches a buyer.

2. Kenyan sellers often price in BULK LOTS, not single items. "3000 for 30
   pairs" means price_kes=3000, unit_quantity=30, unit_label="pairs". If the
   price is for one item, unit_quantity=1. If unstated, leave both null.

3. Record the exact words the price was stated in, in price_evidence — for
   example "@3000 30pairs" or "mia tano". A human uses this to check you.

4. Phone numbers are contacts, NEVER prices. A number like 0712345678 or
   0105515839 is a phone number no matter where it appears.

5. Write name and description in plain English even when the source is in
   Sheng or Swahili.

6. If this is not a sellable product, set is_product to false.
"""

_COVER_PROMPT = f"""You are drafting a product listing for a Kenyan seller's
online shop, from the cover image of one of their TikTok videos.

The price is often printed ON the image, sometimes in Sheng or Swahili
("bei 1500", "1500 only", "@1500").
{_RULES}
Hashtags from the post (context only — they never contain a price): {{hashtags}}
"""

_VIDEO_PROMPT = f"""You are drafting a product listing for a Kenyan seller's
online shop, from one of their TikTok videos.

WATCH the video AND LISTEN to the audio.

Kenyan sellers usually SAY the price out loud rather than writing it, in
English, Swahili or Sheng:
    "mia tano"          = 500
    "elfu moja"         = 1000
    "one five"          = 1500
    "five hundred bob"  = 500
{_RULES}
Hashtags from the post (context only): {{hashtags}}
"""


class DraftAgentError(RuntimeError):
    """
    The vision provider failed in a way the caller must handle.

    Carries the provider's message so a seller-facing retry prompt can say
    something true — a quota exhaustion and a malformed request need different
    responses, and a bare "AI failed" tells nobody anything.
    """


class DraftAgent:
    """Drafts products from media. Owns the provider seam."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._client = genai.Client(api_key=api_key or settings.require("gemini_api_key"))
        self._model = model or settings.gemini_model

    # ── Tier 2: the cover image ──────────────────────────────────────────────

    def draft_from_cover(self, video: TikTokVideo, image_bytes: bytes) -> ProductDraft:
        """
        Read a product and price from a cover image.

        Args:
            video: The scraped video, for hashtag context.
            image_bytes: The cover, already downloaded and type-checked.

        Returns:
            A validated draft. May legitimately carry no price — that is a
            result, not a failure, and it is what triggers escalation.

        Raises:
            DraftAgentError: If the provider fails or returns something that
                cannot be validated.
        """
        return self._generate(
            parts=[types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")],
            prompt=_COVER_PROMPT.format(hashtags=self._hashtags(video)),
            context=f"cover of {video.video_id}",
        )

    # ── Tier 3: the video itself ─────────────────────────────────────────────

    def draft_from_video(self, video: TikTokVideo, video_bytes: bytes) -> ProductDraft:
        """
        Watch and listen to a clip to find a spoken price.

        The most expensive tier, and for some sellers the only one that works.
        Callers must ensure each video reaches this once ever — the result is
        cached against the TikTok video id, never recomputed.

        Args:
            video: The scraped video, for hashtag context.
            video_bytes: The clip, already downloaded and type-checked.

        Returns:
            A validated draft.

        Raises:
            DraftAgentError: If the provider fails or returns invalid output.
        """
        return self._generate(
            parts=[types.Part.from_bytes(data=video_bytes, mime_type="video/mp4")],
            prompt=_VIDEO_PROMPT.format(hashtags=self._hashtags(video)),
            context=f"video {video.video_id}",
        )

    # ── Internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _hashtags(video: TikTokVideo) -> str:
        """
        Hashtags as prompt context.

        They carry category and audience — #sandalsforwomen, #kidscrocs — and
        never a price. Useful as a hint, worthless as a source, and the prompt
        says so explicitly so the model does not mine them for numbers.
        """
        return ", ".join(video.hashtags) or "none"

    def _generate(self, parts: list[types.Part], prompt: str, context: str) -> ProductDraft:
        """
        Call the model with constrained decoding and validate the result.

        Raises:
            DraftAgentError: On provider failure, an empty response, or output
                that fails validation. Never returns a partially-trusted draft.
        """
        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=[*parts, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ProductDraft,
                    temperature=TEMPERATURE,
                ),
            )
        except genai_errors.APIError as exc:
            # Surfaced with the provider's own message: a 429 quota exhaustion
            # and a 400 bad request need different seller-facing responses.
            raise DraftAgentError(f"Vision model failed on {context}: {exc}") from exc

        if not response.text:
            raise DraftAgentError(f"Vision model returned an empty response for {context}")

        try:
            draft = ProductDraft.model_validate_json(response.text)
        except ValueError as exc:
            raise DraftAgentError(
                f"Vision model output failed validation for {context}: {exc}"
            ) from exc

        # Constrained decoding guarantees the SHAPE, never the SENSE. A model
        # can still return a phone number in a price field, and Postgres would
        # reject it far from the cause.
        if not draft.is_plausible:
            raise DraftAgentError(
                f"Vision model returned an implausible price for {context}: "
                f"{draft.price_kes} (evidence: {draft.price_evidence!r})"
            )

        return draft


def get_draft_agent() -> DraftAgent:
    """
    The agent the application uses.

    A function rather than a module-level instance, so importing this module
    never constructs a client or demands an API key.
    """
    return DraftAgent()
