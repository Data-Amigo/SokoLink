"""Pydantic schemas.

Two distinct jobs, deliberately in one place because they use one tool:

1. **Wire schemas** — the shape of API requests and responses. Never the same
   objects as the SQLAlchemy models; leaking a model into a response is how
   password hashes escape.
2. **LLM output schemas** — passed to the model so generation is *constrained*
   to a valid shape. This is the guardrail that replaces parsing-and-praying.
"""
