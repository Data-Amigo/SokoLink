"""SokoLink backend application package.

Layering, enforced by convention:

    api/        HTTP shapes only — parse, validate, delegate, return
    services/   business logic — the part worth testing hardest
    agent/      LLM-facing code — the only place a provider SDK is imported
    models/     SQLAlchemy models — the database rails live here
    schemas/    Pydantic wire and LLM schemas
    templates/  Jinja2 — the storefront

A route that contains business logic, or a service that builds an HTTP
response, is in the wrong layer.
"""
