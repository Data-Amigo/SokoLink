"""
resolve_callback_url() prefers an explicit MPESA_CALLBACK_URL over app_base_url.

THE BUG THIS GUARDS. MPESA_CALLBACK_URL was set in the environment but never
read — the callback was built from app_base_url, which defaulted to localhost
when unset. Safaricom cannot POST to localhost, so a push would be accepted, the
buyer would pay, and the order would never settle. Honouring the explicit URL
removes that trap.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.config import get_settings
from app.services.payments import MPESA_CALLBACK_PATH, resolve_callback_url


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_explicit_callback_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://sokolink-production.up.railway.app/payments/mpesa/callback"
    monkeypatch.setenv("MPESA_CALLBACK_URL", url)

    assert resolve_callback_url() == url


def test_falls_back_to_app_base_url_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MPESA_CALLBACK_URL", raising=False)
    monkeypatch.setenv("APP_BASE_URL", "https://shop.example.com")

    assert resolve_callback_url() == f"https://shop.example.com{MPESA_CALLBACK_PATH}"
