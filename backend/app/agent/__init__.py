"""LLM-facing code.

The ONLY place a model provider SDK is imported. Everything outside this package
sees plain functions and Pydantic objects, so swapping provider or model is a
change in here and nowhere else.

Two rules hold across every module in this package:

  1. **Structured output, not parsed hope.** The model is handed a schema and
     generation is constrained to it. We never ask for JSON and hope.
  2. **The agent proposes; code disposes.** Everything returned is a DRAFT.
     Nothing here writes to the database, sets stock, or publishes.
"""
