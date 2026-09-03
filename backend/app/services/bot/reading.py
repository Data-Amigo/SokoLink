"""
Reading a message the keyword paths could not, via the model.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.understand import UnderstandingError, get_understander
from app.config import settings
from app.models import (
    Seller,
    WaConversation,
)
from app.schemas.conversation import Understanding
from app.services.bot_context import describe


def _understand(
    db: Session,
    convo: WaConversation,
    said: str,
    *,
    owner: Seller | None,
    shopping_at: Seller | None,
) -> Understanding | None:
    """
    Read a message the keyword matcher could not.

    Returns:
        What the model made of it, or None — no API key, a provider failure, an
        unusable answer, or an answer it was not confident enough about.

    Notes:
        NEVER RAISES, and that is the whole contract. This sits in the middle of
        somebody's sale. A quota wall or a timeout must degrade to the keyword
        path we had before, not to an apology, and certainly not to a 500 that
        Meta then redelivers.

        CALLED ONLY ON THE MESSAGES THAT WOULD OTHERWISE HAVE BECOME A SHRUG.
        Buttons carry unambiguous ids, exact commands cost nothing to match, and
        a state expecting a number parses one. What is left is the set that made
        the thread feel like a machine — which is exactly the set worth paying a
        model to read.
    """
    if not settings.gemini_api_key:
        return None

    try:
        reading = get_understander().read(
            said,
            context=describe(db, convo, owner=owner, shopping_at=shopping_at),
        )
    except UnderstandingError:
        return None
    except Exception:  # noqa: BLE001
        # DELIBERATELY BROAD, and this is the one place it is right. The
        # alternative to swallowing an unexpected provider error is a seller
        # watching their shop stop answering because a Google SDK raised
        # something nobody enumerated. The keyword path still works without it.
        return None

    return reading if reading.is_confident else None


def _extracted(db: Session, convo: WaConversation, said: str, owner: Seller, field: str) -> str:
    """
    The answer inside a sentence, or the sentence when there is no model.

    Args:
        field: Which field of the reading to take — "name" or "about".

    Notes:
        WHY THIS EXISTS SEPARATELY. Asking a question tells us what the reply
        is ABOUT; it does not make the reply only the answer. People wrap it:
        "My shop name should be Biggie Books", "call it Biggie Books", "it's
        Biggie Books". Taking the raw text names the shop after the sentence,
        which is exactly the failure the extraction was built for.

        FALLS BACK TO THE RAW TEXT, because a shop that cannot be renamed
        without a working model is worse than one renamed clumsily.
    """
    reading = _understand(db, convo, said, owner=owner, shopping_at=None)
    if reading is not None:
        value = getattr(reading, field, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return said
