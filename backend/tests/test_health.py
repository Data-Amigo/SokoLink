"""
Tests for the health endpoints.

These prove the P0 "done when": the app boots and can genuinely reach its own
database. The readiness test is the one that matters — it is the difference
between "the process started" and "the process can serve a request".
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.config import get_settings, settings
from app.db import get_db
from app.main import app


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == settings.app_name


def test_health_does_not_touch_the_database(client: TestClient) -> None:
    """
    Liveness must answer even when Postgres is unreachable.

    If it did not, a database outage would make the platform restart a perfectly
    healthy process in a loop.
    """

    def _broken_db() -> object:
        raise AssertionError("liveness must not open a database connection")

    app.dependency_overrides[get_db] = _broken_db
    try:
        assert client.get("/health").status_code == 200
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_ready_reports_database_up(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "up"
    assert body["detail"] is None


def test_ready_reports_503_when_database_is_down(client: TestClient) -> None:
    """A failed database check must return 503, not crash and not lie."""

    class _FailingSession:
        def execute(self, *_args: object, **_kwargs: object) -> None:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    def _override() -> object:
        yield _FailingSession()

    app.dependency_overrides[get_db] = _override
    try:
        response = client.get("/health/ready")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "down"
    assert "connection refused" in body["detail"]


class TestTheRunningVersion:
    """
    /health names the commit it is running.

    WHY THIS EARNED A FIELD. "Is my code actually live?" was answered wrongly
    twice in this project: once because a stale worker held the port and served
    hours-old code, once because a push went to a branch nothing deploys. Both
    times the only way to tell was to infer it from behaviour, which is how a
    wrong answer gets stated confidently.
    """

    def test_it_reports_the_commit(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "railway_git_commit_sha", "abc1234def5678")

        body = client.get("/health").json()

        assert body["version"] == "abc1234"

    def test_outside_a_build_it_says_unknown(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A developer machine has no commit injected, and pretending otherwise
        would make the field untrustworthy where it matters."""
        monkeypatch.setattr(get_settings(), "railway_git_commit_sha", None)

        assert client.get("/health").json()["version"] == "unknown"
