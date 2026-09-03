"""
The briefing the model reads before it interprets a message.

``describe`` turns a conversation into plain prose that lets the model tell a
seller naming their shop from a buyer naming a product. Phase 4 gave it two more
things the model previously had no way to know: what was said just before, and
whether a batch of forwarded photos is still being read — the gap that produced
the "I'm not quite sure what you're referring to" shrug in the screenshots.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import WaMessage
from app.services.bot import get_conversation
from app.services.bot_context import describe
from tests.factories import make_seller

PHONE = "254712345678"


def _record(db: Session, body: str, *, minutes_ago: int) -> None:
    """Store an inbound message as the webhook would, at a chosen time."""
    db.add(
        WaMessage(
            provider_message_id=f"wamid.{minutes_ago}.{body[:6]}",
            from_number=PHONE,
            body=body,
            media_count=0,
            raw={},
            created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        )
    )
    db.flush()


class TestTheBriefingCarriesTheThread:
    def test_earlier_messages_are_included_oldest_first(self, db: Session) -> None:
        seller = make_seller(db, whatsapp_number=PHONE)
        convo = get_conversation(db, PHONE)
        _record(db, "do you have shoes", minutes_ago=5)
        _record(db, "size 40", minutes_ago=3)
        _record(db, "the black ones", minutes_ago=1)  # the message being read now

        briefing = describe(db, convo, owner=seller, shopping_at=None)

        assert "do you have shoes" in briefing
        assert "size 40" in briefing
        # The most recent row IS the current message; the model already has it.
        assert "the black ones" not in briefing
        assert briefing.index("do you have shoes") < briefing.index("size 40")

    def test_no_history_line_when_there_is_none(self, db: Session) -> None:
        """A conversation driven directly, with no webhook, has nothing to quote."""
        seller = make_seller(db, whatsapp_number=PHONE)
        convo = get_conversation(db, PHONE)

        briefing = describe(db, convo, owner=seller, shopping_at=None)

        assert "Earlier messages" not in briefing


class TestTheBriefingKnowsAboutABatch:
    def test_it_says_photos_are_still_being_read(self, db: Session) -> None:
        seller = make_seller(db, whatsapp_number=PHONE)
        convo = get_conversation(db, PHONE)
        convo.context = {"intaking": True}

        briefing = describe(db, convo, owner=seller, shopping_at=None)

        assert "still being added" in briefing
