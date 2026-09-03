"""
Reading what somebody actually meant, when the keywords did not match.

    buttons + exact commands ──▶ handled by code, free, instant, unambiguous
                                        │
                                   no match
                                        ▼
    message + who they are + what the shop has ──▶ Gemini ──▶ Understanding
                                                                  │
                                              code decides and acts on it

WHY THIS EXISTS, in one exchange from a real handset:

    bot   What's your shop called?
    them  My shop is called Biggie Books
    bot   *My shop is called Biggie Books* is yours. 🎉

The whole sentence became the shop's name, because the conversation was a list
of ``if lowered in {...}`` sets and nothing in it could read English. They then
corrected it twice — "Sorry I mean Biggie Books is my brand name", "A way to put
my catalogue and my shop link" — and got the same status card both times,
because neither phrase matched a keyword either.

No amount of adding keywords fixes that. People do not speak in commands.

WHAT IT IS ALLOWED TO DO. Choose one of a fixed set of intents, pull the useful
words out of the sentence, and — for greetings and small talk only — write the
reply itself. It never decides a price, stock, an order's state or whether
somebody has paid. Those come from the database, and the reason is not
philosophical: a model that guesses a price reaches a buyer with it.

WHEN IT IS NOT CALLED, which is most of the time:

    a button or list tap        the id is already unambiguous
    an exact command            "menu", "cart", "orders" cost nothing to match
    a state expecting a number  a price or an M-Pesa code is parsed, not read

So the spend is one small call on the messages that would otherwise have become
a shrug — which is exactly the set that made the thread feel like a machine.

DEGRADES TO WHAT WE HAD. No API key, a quota wall, a timeout, a malformed
answer: every one of them returns None and the caller falls back to the keyword
path. The shop keeps working without the model, which is the only acceptable
relationship to have with a paid dependency in the middle of somebody's sale.
"""

from __future__ import annotations

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.config import settings
from app.schemas.conversation import Understanding

#: Interpretation, not invention. The same sentence should read the same way
#: twice, and a model being creative about what a seller meant is a bug.
TEMPERATURE = 0.1

#: A long message is a story, not a command, and the useful part is at the
#: front. Also a cost ceiling: nobody types a 4,000-character shop name.
MAX_INPUT_CHARS = 600

_PERSONA = """
You are the assistant inside Biashara Mall, a WhatsApp shop used by small
Kenyan traders and their customers. You are reading ONE message and deciding
what the person wants.

How people here actually write:

- Sentences, not commands. "My shop is called Biggie Books" is the name
  "Biggie Books" wrapped in a sentence. Strip the wrapper.
- Corrections come later and out of nowhere: "sorry I mean X", "no, it's X".
  Treat those as the same intent as the original answer, with the new value.
- English, Swahili and Sheng mix freely. "Niaje", "sasa", "bei ni ngapi",
  "si zaidi ya elfu moja", "nataka viatu". Read all of it.
- Money is said many ways: "1k", "elfu moja", "1,000 bob", "one thousand".
  Always return whole shillings as a number.

Rules, in order of importance:

1. NEVER state a price, a stock level, whether something is available, or the
   state of an order. You cannot see any of those. Code reads them from the
   database and writes those sentences. If somebody asks "do you have X", that
   is FIND_PRODUCT with query "X" — it is not a question for you to answer.

2. Choose UNKNOWN when you are genuinely unsure. A wrong confident answer sends
   somebody down the wrong path; UNKNOWN shows them their options. Unsure is a
   normal, correct answer.

3. Only fill `reply` for GREET, HELP, SMALL_TALK and UNKNOWN, and keep it to two
   sentences. Warm, plain, like a shopkeeper who is busy but glad you came in.
   No emoji storms, no exclamation marks stacked up, no "As an AI".

4. Extract the entity for the intent you chose and leave the others null.
"""


class UnderstandingError(RuntimeError):
    """The model could not be reached, or said something unusable."""


class Understander:
    """Reads one message in the context of one conversation."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._client = genai.Client(api_key=api_key or settings.require("gemini_api_key"))
        self._model = model or settings.gemini_model

    def read(self, message: str, *, context: str) -> Understanding:
        """
        Work out what one message means.

        Args:
            message: What the person sent, verbatim.
            context: Who they are and what the shop holds — see
                :func:`app.services.bot_context.describe`. This is what lets the
                model tell a seller naming their shop from a buyer naming a
                product.

        Returns:
            A validated :class:`Understanding`.

        Raises:
            UnderstandingError: On any provider failure or unusable output. The
                caller falls back to keyword matching rather than apologising to
                somebody mid-purchase.
        """
        prompt = (
            f"{_PERSONA}\n\n"
            f"--- Who you are talking to ---\n{context}\n\n"
            f"--- Their message ---\n{message[:MAX_INPUT_CHARS]}\n"
        )

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=Understanding,
                    temperature=TEMPERATURE,
                ),
            )
        except genai_errors.APIError as exc:
            raise UnderstandingError(f"Could not read the message: {exc}") from exc

        if not response.text:
            raise UnderstandingError("The model returned nothing")

        try:
            return Understanding.model_validate_json(response.text)
        except ValueError as exc:
            raise UnderstandingError(f"The model's answer failed validation: {exc}") from exc


def get_understander() -> Understander:
    """
    The understander the application uses.

    A function rather than a module-level instance, so importing this module
    never constructs a client or demands an API key — the same seam the draft
    agent uses, and the reason tests can swap it out without a network.
    """
    return Understander()
