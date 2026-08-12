"""Test suite.

A package rather than loose modules, so shared helpers live in `factories.py`
and are imported explicitly. Without `__init__.py`, importing across test
modules makes the same file resolve under two module names and mypy rejects it —
which is a fair complaint: test modules should not import each other.
"""
