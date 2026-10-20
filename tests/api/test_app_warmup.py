"""Warming up the AI client in lifespan does not break app startup.

The warm-up pays the SDK import at process start, not on the first click on “Translate”.
The network is not touched. If the warm-up fails (no key, no package), the application must
come up: ``/healthz`` = 200, and only the exception type goes to the log (without the error text).
Mutation check: remove the try/except in ``_lifespan`` → the test turns red.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from src.ai import translation
from src.api.main import app


def test_app_starts_when_warmup_raises(monkeypatch, caplog):
    def _boom() -> None:
        raise RuntimeError("secret-must-not-reach-the-log")

    monkeypatch.setattr(translation, "warm_default_client", _boom)

    with caplog.at_level(logging.WARNING, logger="src.api.main"):
        with TestClient(app) as client:
            resp = client.get("/healthz")

    assert resp.status_code == 200
    assert "RuntimeError" in caplog.text
    assert "секрет-не-должен-попасть-в-лог" not in caplog.text


def test_lifespan_calls_warmup_once_on_start(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        translation, "warm_default_client", lambda: calls.append("warm")
    )

    with TestClient(app):
        pass

    assert calls == ["warm"]
