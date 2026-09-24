"""Global test safety guards."""

from __future__ import annotations

import gc

import pytest
import requests

import database
import logic
from services import auth_service, course_cache_service


@pytest.fixture(autouse=True)
def isolate_captcha_files(monkeypatch, tmp_path):
    """Never overwrite a running user's captcha while exercising OCR helpers."""
    monkeypatch.setattr(logic, "_captcha_image_path", lambda: tmp_path / "captcha.jpg")


@pytest.fixture(autouse=True)
def block_unmocked_network(monkeypatch):
    """Fail fast if a test reaches the real network."""

    def blocked_request(*args, **kwargs):
        raise AssertionError("Tests must mock every external HTTP request")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked_request)


@pytest.fixture(autouse=True)
def isolate_course_cache(monkeypatch, tmp_path):
    """Keep persistent course-cache tests out of the repository workspace."""
    monkeypatch.setattr(course_cache_service, "_path", tmp_path / "course_cache.json")


@pytest.fixture(autouse=True)
def isolate_relogin_state():
    """Keep process-wide automatic recovery counters isolated per test."""
    with auth_service._state_lock:
        auth_service._reset_relogin_state_locked()
    yield


@pytest.fixture(autouse=True)
def close_database_connections():
    """Close every SQLite manager after each test instead of relying on GC."""
    yield
    database.DatabaseManager.close_all()
    gc.collect()
