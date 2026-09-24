"""Scheduled undergraduate starts use Beijing time and the guarded start path."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from starlette.testclient import TestClient

import app
import logic
from services import scheduled_enroll, school_clock


@pytest.fixture(autouse=True)
def clear_schedule():
    scheduled_enroll.cancel()
    with scheduled_enroll._lock:
        scheduled_enroll._state.update(
            status="idle", target_at="", student_id="", mode="", message=""
        )
    with school_clock._lock:
        school_clock._sample = None
    yield
    scheduled_enroll.cancel()


def test_target_requires_explicit_beijing_offset_and_future_window():
    target = datetime.now(UTC) + timedelta(hours=1)
    assert scheduled_enroll.parse_target(target.astimezone(scheduled_enroll.BEIJING).isoformat())
    with pytest.raises(ValueError, match="时区"):
        scheduled_enroll.parse_target(target.replace(tzinfo=None).isoformat())
    with pytest.raises(ValueError, match="30 秒"):
        scheduled_enroll.parse_target(
            (datetime.now(UTC) + timedelta(seconds=2))
            .astimezone(scheduled_enroll.BEIJING)
            .isoformat()
        )


def test_school_date_advances_with_monotonic_clock(monkeypatch):
    base = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(school_clock.time, "monotonic", lambda: 100.0)
    school_clock.observe(format_datetime(base), 100.0)
    monkeypatch.setattr(school_clock.time, "monotonic", lambda: 102.5)
    current, source = school_clock.now()
    assert source == "school"
    assert current == base + timedelta(seconds=2.5)


def test_school_batch_endpoint_matches_current_batch_and_parses_beijing_time(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = "school batch"
        headers = {}

        def json(self):
            return {
                "code": "1",
                "dataList": [
                    {"code": "old", "beginTime": "2026-09-24 09:00:00"},
                    {"code": "current", "beginTime": "2026-09-24 12:30:00"},
                ],
            }

        def raise_for_status(self):
            return None

    def school_request(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return Response()

    monkeypatch.setattr(logic, "_school_request", school_request)
    start = logic.fetch_undergraduate_start_time("current", "token", "cookie")
    assert start == datetime(2026, 9, 24, 4, 30, tzinfo=UTC)
    assert captured["method"] == "POST"
    assert captured["path"].startswith("elective/batch.do?timestamp=")
    assert captured["read_only"] is True
    assert captured["cookie"] == "cookie"


def test_school_batch_endpoint_rejects_ambiguous_batch(monkeypatch):
    class Response:
        status_code = 200
        text = "school batch"
        headers = {}

        def json(self):
            return {"code": "1", "dataList": [{"beginTime": "2026-09-24 12:30:00"}] * 2}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(logic, "_school_request", lambda *_args, **_kwargs: Response())
    with pytest.raises(logic.SchoolStartTimeUnavailableError, match="当前选课批次"):
        logic.fetch_undergraduate_start_time("current", "token", "cookie")


def test_schedule_can_be_armed_before_phase_opens_and_cancelled(monkeypatch):
    monkeypatch.setattr(app, "is_graduate", lambda: False)
    monkeypatch.setattr(
        app,
        "get_session_snapshot",
        lambda: {"logged_in": True, "student_id": "student-1"},
    )
    monkeypatch.setattr(app, "is_enroll_task_running", lambda: False)
    monkeypatch.setattr(
        app.cart_service, "get_courses_by_status", lambda _status: [{"auto_enabled": 1}]
    )
    target = (datetime.now(UTC) + timedelta(minutes=2)).astimezone(scheduled_enroll.BEIJING)
    client = TestClient(app.app)
    response = client.post(
        "/api/enroll/schedule",
        json={"target_at": target.isoformat(), "confirmed_phase": True},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "armed"
    assert client.get("/api/enroll/schedule").json()["student_id"] == "student-1"
    assert client.post("/api/enroll/schedule/cancel").json()["status"] == "idle"


def test_due_schedule_calls_guarded_start_once(monkeypatch):
    monkeypatch.setattr(app, "is_graduate", lambda: False)
    monkeypatch.setattr(
        app,
        "get_session_snapshot",
        lambda: {"logged_in": True, "student_id": "student-1"},
    )
    monkeypatch.setattr(app, "is_enroll_task_running", lambda: False)
    monkeypatch.setattr(
        app.cart_service, "get_courses_by_status", lambda _status: [{"auto_enabled": 1}]
    )
    started = threading.Event()
    calls = []

    async def guarded_start(request, expected_batch_code=None):
        calls.append(request.confirmed_phase)
        assert expected_batch_code is None
        started.set()
        return app.ApiMessage(message="抢课任务已在后台启动", is_error=False)

    monkeypatch.setattr(app, "_start_enrollment", guarded_start)
    monkeypatch.setattr(
        scheduled_enroll,
        "parse_target",
        lambda _value: datetime.now(UTC) + timedelta(milliseconds=100),
    )
    client = TestClient(app.app)
    response = client.post(
        "/api/enroll/schedule",
        json={"target_at": "2026-09-24T12:00:00+08:00", "confirmed_phase": True},
    )
    assert response.status_code == 200
    assert started.wait(2)
    deadline = time.monotonic() + 2
    while scheduled_enroll.status()["status"] == "starting" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert scheduled_enroll.status()["status"] == "started"
    assert calls == [True]


def test_school_mode_tracks_revised_start_time(monkeypatch):
    original = datetime.now(UTC) + timedelta(minutes=1)
    revised = original + timedelta(minutes=2)
    clock = {"now": original - timedelta(seconds=60)}
    started = threading.Event()

    monkeypatch.setattr(school_clock, "now", lambda: (clock["now"], "school"))

    def sync_clock():
        clock["now"] = revised + timedelta(seconds=1)
        return revised

    scheduled_enroll.arm(
        original,
        "student-1",
        lambda: (started.set() or True, "started"),
        sync_clock,
        mode="school",
    )
    assert started.wait(2)
    assert scheduled_enroll.status()["target_at"] == revised.astimezone(
        scheduled_enroll.BEIJING
    ).isoformat(timespec="seconds")


def test_school_mode_displays_time_and_requires_reconfirmation_if_it_changes(monkeypatch):
    monkeypatch.setattr(app, "is_graduate", lambda: False)
    monkeypatch.setattr(
        app,
        "get_session_snapshot",
        lambda: {"logged_in": True, "student_id": "student-1"},
    )
    monkeypatch.setattr(app, "is_enroll_task_running", lambda: False)
    monkeypatch.setattr(
        app.cart_service, "get_courses_by_status", lambda _status: [{"auto_enabled": 1}]
    )
    school_time = (datetime.now(UTC) + timedelta(minutes=3)).astimezone(
        scheduled_enroll.BEIJING
    ).isoformat(timespec="seconds")

    async def get_school_time():
        return {"target_at": school_time, "batch_name": "正选", "batch_code": "B1"}

    monkeypatch.setattr(app, "_get_school_start_time", get_school_time)
    client = TestClient(app.app)
    assert client.get("/api/enroll/school-start-time").json() == {
        "target_at": school_time,
        "batch_name": "正选",
        "batch_code": "B1",
    }
    changed = client.post(
        "/api/enroll/schedule",
        json={
            "mode": "school",
            "expected_target_at": "2026-09-24T12:00:00+08:00",
            "confirmed_phase": True,
        },
    )
    assert changed.status_code == 409
    assert changed.json()["target_at"] == school_time
    armed = client.post(
        "/api/enroll/schedule",
        json={"mode": "school", "expected_target_at": school_time, "confirmed_phase": True},
    )
    assert armed.status_code == 200
    assert armed.json()["mode"] == "school"
    assert armed.json()["target_at"] == school_time
