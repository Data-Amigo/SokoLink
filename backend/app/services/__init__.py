"""Business logic.

Services own the rules and the transactions. They take and return plain Python
objects and SQLAlchemy models — never FastAPI request or response objects — so
they can be tested without HTTP and reused from a webhook, a route, or a job.

External providers sit behind our own interface here (the adapter pattern), so
swapping a scrape engine or a payment provider is a one-file change and callers
never notice.
"""
