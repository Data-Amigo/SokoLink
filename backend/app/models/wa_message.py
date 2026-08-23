"""
One inbound WhatsApp message, recorded so it is never processed twice.

    Twilio ──▶ WaMessage(provider_message_id UNIQUE) ──▶ (later) a product

WHY THIS TABLE EXISTS BEFORE ANYTHING READS IT. Every WhatsApp provider
redelivers: Twilio retries any webhook that is slow, errors or times out, and
Meta does the same. When a forwarded photo becomes a product, a redelivery
without this table becomes a SECOND product from one message — a seller's
catalogue quietly doubling itself.

The unique constraint is the guard, not a check in a handler. A handler that
looks first and inserts second is a race; two retries arriving together both
see nothing and both insert. Postgres refusing the duplicate cannot race.

THE RAW PAYLOAD IS KEPT VERBATIM. When a forwarded catalogue parses into the
wrong products, this is the only evidence of what actually arrived — and it is
the replay input when the parsing prompt changes. A parsed summary is an opinion
about the message; this is the message.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WaMessage(Base):
    """An inbound message, and enough to recognise it arriving twice."""

    __tablename__ = "wa_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: The provider's own id — Twilio's MessageSid. UNIQUE, and the entire
    #: idempotency story.
    provider_message_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    #: Bare 2547XXXXXXXX, with the channel prefix stripped, so it matches
    #: Account.phone and Seller.whatsapp_number without every query remembering
    #: to strip "whatsapp:+".
    from_number: Mapped[str] = mapped_column(String(20), nullable=False)

    #: The caption or text. NULL for a photo sent with nothing written.
    body: Mapped[str | None] = mapped_column(Text)

    #: How many attachments came with it. A forwarded catalogue is often several
    #: images in one message, which is why this is a count and not a boolean.
    media_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Exactly what the provider posted. See the module docstring.
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    #: Set when this message has been turned into whatever it was going to be.
    #: NULL means "arrived, not yet acted on" — which is also the retry queue.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("media_count >= 0", name="ck_wa_messages_media_count_non_negative"),
        # "What has this seller sent us lately", and the unprocessed backlog.
        Index("ix_wa_messages_from_created", "from_number", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<WaMessage {self.provider_message_id} from={self.from_number} "
            f"media={self.media_count}>"
        )
