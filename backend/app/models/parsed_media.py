"""
Every forwarded image we have paid to read, and what the model said.

    provider media id ──▶ ParsedMedia(draft JSON) ──▶ never parsed again

WHY A CACHE TABLE AND NOT A MEMO IN PROCESS. This is the only paid call in the
forwarding path, and the traffic pattern is the worst possible one for it: a
seller onboarding forwards their whole catalogue in a burst, Twilio redelivers
anything slow, and a dyno restart empties any in-memory cache. Keyed by the
provider's media id, an image is read once ever — across retries, restarts and
redeploys.

IT STORES THE ANSWER, NOT THE IMAGE. Bytes are large, we already have the media
URL, and what costs money is the inference, not the download. Storing the draft
also means a re-run can be replayed and compared when the prompt changes.

A FAILURE IS CACHED TOO, deliberately, in ``error``. Without that, an image the
model reliably chokes on is re-sent and re-billed on every redelivery. It is
kept separate from a successful draft so a retry can be a deliberate act rather
than an accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ParsedMedia(Base):
    """One image, read once."""

    __tablename__ = "parsed_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: The provider's own id for this media. Unique, and the entire point of
    #: the table — it is what makes "once ever" enforceable by Postgres rather
    #: than by everyone remembering to check first.
    provider_media_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    #: The validated ProductDraft as JSON, or NULL when the read failed.
    draft: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    #: The provider's message when it failed. Cached so a redelivery does not
    #: re-bill us for an image that reliably fails.
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Exactly one of the two, always. A row with neither records nothing and
        # would be re-parsed forever; a row with both cannot be interpreted.
        CheckConstraint(
            "(draft IS NULL) <> (error IS NULL)",
            name="ck_parsed_media_draft_xor_error",
        ),
    )

    def __repr__(self) -> str:
        outcome = "draft" if self.draft else "error"
        return f"<ParsedMedia {self.provider_media_id} {outcome}>"
