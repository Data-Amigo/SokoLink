"""
Forwarding is asynchronous, and that is the fix.

Before this, a seller who forwarded a catalogue watched it trickle in one item
at a time over an hour, out of order, because every photo was a paid vision call
made inside the webhook while Meta redelivered anything slow. Now the webhook
enqueues one job per photo and answers at once; the worker parses off the
request path; and the LAST job of the burst sends ONE summary.

These prove the three guarantees that make that safe and calm:

    the webhook does not parse — it enqueues and acknowledges once
    a burst produces one summary, not one message per photo
    a redelivered photo is not parsed (or billed) twice
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.draft import DraftAgentError
from app.models import ConversationState, Job, JobStatus, Product
from app.schemas.draft import ProductDraft
from app.services import intake, whatsapp_cloud
from app.services.bot import get_conversation, handle
from app.services.intake import PARSE_FORWARD
from tests.factories import make_seller

SELLER = "254712345678"


def a_draft(name: str, **overrides: Any) -> ProductDraft:
    values: dict[str, Any] = {
        "is_product": True,
        "name": name,
        "description": "Forwarded stock.",
        "price_kes": None,
        "unit_quantity": None,
        "unit_label": None,
        "price_evidence": None,
        "confidence": 0.3,
    }
    values.update(overrides)
    return ProductDraft.model_validate(values)


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that names each photo in turn, and media that fetches to bytes."""
    names = iter(["Ankara Shirt", "Beaded Belt", "Canvas Tote", "Denim Jacket"])

    class Fake:
        @staticmethod
        def draft_from_forwarded(caption: str, image: bytes) -> ProductDraft:
            return a_draft(next(names))

    monkeypatch.setattr(intake, "get_draft_agent", lambda: Fake())
    monkeypatch.setattr(whatsapp_cloud, "download_media", lambda media_id: b"\xff\xd8jpeg")


def forward(db: Session, count: int, caption: str = "New stock") -> Any:
    """One inbound message carrying ``count`` photos."""
    media = [(f"m{i}", (lambda: b"jpeg")) for i in range(count)]
    return handle(db, SELLER, caption, media=media)


def test_the_webhook_enqueues_and_does_not_parse_in_the_request(db: Session, parses: None) -> None:
    make_seller(db, whatsapp_number=SELLER)

    outcome = forward(db, 3)

    # Nothing has been read yet — the parse is on the queue, not in the request.
    assert db.scalars(select(Product)).all() == []
    queued = db.scalar(
        select(func.count(Job.id)).where(
            Job.kind == PARSE_FORWARD, Job.status == JobStatus.QUEUED.value
        )
    )
    assert queued == 3
    # One acknowledgement, not three.
    assert len(outcome.replies) == 1
    assert "reading" in outcome.replies[0].body.lower()


def test_a_second_forward_in_the_burst_is_not_acknowledged_again(db: Session, parses: None) -> None:
    make_seller(db, whatsapp_number=SELLER)

    first = forward(db, 1)
    second = forward(db, 1)

    assert len(first.replies) == 1
    assert second.replies == []


def test_the_last_job_sends_one_summary_for_the_whole_burst(
    db: Session, parses: None, run_jobs: Any, sent: list[tuple[str, Any]]
) -> None:
    make_seller(db, whatsapp_number=SELLER)
    forward(db, 3)

    run_jobs()

    # ONE message for three photos — the trickle is gone.
    assert len(sent) == 1
    to, reply = sent[0]
    assert to == SELLER
    assert "(3 left)" in reply.body
    assert get_conversation(db, SELLER).state == ConversationState.PRICING
    assert len(db.scalars(select(Product)).all()) == 3


def test_a_redelivered_photo_is_not_enqueued_twice(db: Session, parses: None) -> None:
    make_seller(db, whatsapp_number=SELLER)

    handle(db, SELLER, "New stock", media=[("dup", (lambda: b"jpeg"))])
    handle(db, SELLER, "New stock", media=[("dup", (lambda: b"jpeg"))])

    jobs = db.scalar(select(func.count(Job.id)).where(Job.kind == PARSE_FORWARD))
    assert jobs == 1


def test_a_burst_that_cannot_be_read_is_told_not_left_silent(
    db: Session, monkeypatch: pytest.MonkeyPatch, run_jobs: Any, sent: list[tuple[str, Any]]
) -> None:
    """Every photo failing is a result, not silence — the seller hears it once."""

    class Boom:
        @staticmethod
        def draft_from_forwarded(caption: str, image: bytes) -> ProductDraft:
            raise DraftAgentError("could not read")

    monkeypatch.setattr(intake, "get_draft_agent", lambda: Boom())
    monkeypatch.setattr(whatsapp_cloud, "download_media", lambda media_id: b"\xff\xd8jpeg")
    make_seller(db, whatsapp_number=SELLER)

    handle(db, SELLER, "New stock", media=[("m0", (lambda: b"jpeg"))])
    run_jobs()

    assert db.scalars(select(Product)).all() == []
    assert len(sent) == 1
    assert "couldn't read" in sent[0][1].body.lower()
