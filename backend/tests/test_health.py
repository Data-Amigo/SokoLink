"""
Tests for the health endpoints.

These prove the P0 "done when": the app boots and can genuinely reach its own
database. The readiness test is the one that matters — it is the difference
between "the process started" and "the process can serve a request".
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.config import settings
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
