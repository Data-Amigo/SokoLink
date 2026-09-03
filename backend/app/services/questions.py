"""
Handing a customer's question to the shop, and the answer back.

    buyer asks something we cannot know
            │
            ├──▶ BuyerQuestion (open)  ──▶ the seller's thread
            │                                      │
            └──▶ "Let me ask them"            they answer
                                                   │
    "Vitabu Bora says: …"  ◀────────────────────────┘

WHY THERE IS NO "I DON'T KNOW" IN HERE. It is not an answer a shop gives, and it
is not a state we ship. A buyer asking "do you deliver to Kisumu?" is asking
something only the seller knows — we hold no delivery zones, no fees, no lead
times — so the question is handed over rather than guessed at or shrugged off.

THE SELLER'S WORDS ARE RELAYED VERBATIM. Not summarised, not improved, not
written on their behalf. The moment we author a sentence about delivery or
availability we have made a commitment on somebody else's business, and the
buyer has no way to tell which of us said it.

THE BUYER'S PLACE IS NEVER LOST. Asking a question does not move their
conversation — somebody halfway through choosing a size is still choosing a
size, and the answer arrives beside that rather than instead of it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BuyerQuestion, Seller

#: How many open questions to show a seller at once. A list picker holds ten,
#: and past that they have a backlog the chat cannot help with.
OPEN_LIMIT = 10

#: Longest question we will carry. Past this somebody is telling a story, and
#: the seller reads it better in their own thread than in a row title.
MAX_QUESTION_CHARS = 900

#: Longest answer we will relay. Generous — a seller explaining delivery to
#: Kisumu may need a paragraph — but bounded, because it becomes a message.
MAX_ANSWER_CHARS = 1500


def ask(db: Session, seller: Seller, *, buyer_phone: str, question: str) -> BuyerQuestion:
    """
    Record a question for the shop to answer.

    Args:
        db: Session. The caller commits.
        seller: The shop being asked.
        buyer_phone: Who is waiting for the answer.
        question: What they asked, in their own words.

    Returns:
        The open question.

    Notes:
        VERBATIM, not paraphrased. The seller is answering a person, and every
        rewrite between the two of them is somewhere the meaning can shift.
    """
    row = BuyerQuestion(
        seller_id=seller.id,
        buyer_phone=buyer_phone,
        question=question.strip()[:MAX_QUESTION_CHARS],
    )
    db.add(row)
    db.flush()
    return row


def open_questions(db: Session, seller: Seller, limit: int = OPEN_LIMIT) -> list[BuyerQuestion]:
    """
    What customers are still waiting on this shop to answer.

    Oldest first, deliberately: the person who has been waiting longest is the
    one to answer next, and a newest-first list quietly buries them.
    """
    return list(
        db.scalars(
            select(BuyerQuestion)
            .where(
                BuyerQuestion.seller_id == seller.id,
                BuyerQuestion.answered_at.is_(None),
            )
            .order_by(BuyerQuestion.created_at)
            .limit(limit)
        ).all()
    )


def oldest_open(db: Session, seller: Seller) -> BuyerQuestion | None:
    """The one to answer next, or None when nobody is waiting."""
    found = open_questions(db, seller, limit=1)
    return found[0] if found else None


def get_for_seller(db: Session, seller: Seller, question_id: int) -> BuyerQuestion | None:
    """
    One question, if it belongs to this shop.

    SCOPED, and the check is the point rather than a formality. Without it any
    seller could answer another shop's customer, in that shop's name, about
    that shop's stock — and the buyer would have no way to tell.
    """
    return db.scalar(
        select(BuyerQuestion).where(
            BuyerQuestion.id == question_id,
            BuyerQuestion.seller_id == seller.id,
        )
    )


def answer(db: Session, question: BuyerQuestion, text: str) -> BuyerQuestion:
    """
    Close a question with the seller's own words.

    Args:
        db: Session. The caller commits.
        question: The open question, already checked as this seller's.
        text: What the seller wrote.

    Returns:
        The answered question.

    Raises:
        ValueError: If it is already answered, or the answer is blank. The
            database enforces both too — an answered row with no words in it
            would relay an empty message to somebody waiting on a real one.
    """
    cleaned = " ".join(text.split())[:MAX_ANSWER_CHARS]
    if not cleaned:
        raise ValueError("An answer needs some words in it.")
    if question.answered_at is not None:
        raise ValueError("That question has already been answered.")

    question.answer = cleaned
    question.answered_at = datetime.now(UTC)
    db.flush()
    return question
