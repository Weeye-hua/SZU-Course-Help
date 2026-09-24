"""Appointments never submit enrollment without the existing guarded start path."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests
from starlette.testclient import TestClient

import app
import config
import logic
from services import auth_service, scheduled_enroll, school_clock

NOW = datetime(2026, 9, 24, 4, 0, tzinfo=UTC)


def beijing(value):
    return value.astimezone(scheduled_enroll.BEIJING).isoformat()


def wait_status(expected):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = scheduled_enroll.status()
        if state["status"] == expected:
            return state
        time.sleep(0.01)
    pytest.fail(f"Expected {expected}: {scheduled_enroll.status()}")


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(app, "is_graduate", lambda: False)
    for key, value in {
        "token": "token",
        "combined_cookie": "cookie",
        "student_id": "2026000001",
        "password": "test-only",
        "elective_batch_code": "B1",
        "elective_batch_name": "正选",
    }.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(app, "refresh_elective_batch", lambda *_: "正选")
    monkeypatch.setattr(app, "is_enroll_task_running", lambda: False)
    rows = [{"id": "C1", "auto_enabled": 1}]
    monkeypatch.setattr(app.cart_service, "get_courses_by_status", lambda _: rows)
    launches = []
    monkeypatch.setattr(app, "start_enroll_worker", lambda: launches.append(True) or True)
    return TestClient(app.app), rows, launches


def appointment(**overrides):
    return {
        "mode": "custom",
        "confirmed_phase": True,
        "target_at": beijing(datetime.now(UTC) + timedelta(minutes=10)),
        "batch_code": "B1",
        "expected_revision": scheduled_enroll.status()["revision"],
        "expected_instance": scheduled_enroll.status()["instance_id"],
        **overrides,
    }


def cancellation(revision=None, instance=None):
    state = scheduled_enroll.status()
    return {
        "expected_revision": state["revision"] if revision is None else revision,
        "expected_instance": state["instance_id"] if instance is None else instance,
    }


@pytest.mark.parametrize(
    "seconds,valid", [(29, False), (30, True), (604800, True), (604801, False), (-1, False)]
)
def test_target_window(monkeypatch, seconds, valid):
    monkeypatch.setattr(school_clock, "now", lambda: (NOW, "local"))
    value = beijing(NOW + timedelta(seconds=seconds))
    if valid:
        assert scheduled_enroll.parse_target(value) == NOW + timedelta(seconds=seconds)
    else:
        with pytest.raises(ValueError, match="30 秒"):
            scheduled_enroll.parse_target(value)


@pytest.mark.parametrize("value", ["bad", None, "2026-09-24T13:00:00", "2026-09-24T13:00:00Z"])
def test_target_requires_explicit_beijing_timezone(value):
    with pytest.raises(ValueError):
        scheduled_enroll.parse_target(value)


def test_school_clock_advances_and_expires(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(school_clock.time, "monotonic", lambda: clock["t"])
    school_clock.observe(format_datetime(NOW), 100, sent_at=99)
    clock["t"] = 102.5
    assert school_clock.now() == (NOW + timedelta(seconds=2.5), "school")
    clock["t"] = 701
    assert school_clock.now()[1] == "local"


@pytest.mark.parametrize(
    "header,received,sent,age",
    [
        ("bad", 100, 99, None),
        (None, 100, 99, None),
        (format_datetime(NOW), 100, 80, None),
        (format_datetime(NOW), 100, 101, None),
        (format_datetime(NOW), 100, 99, "12"),
        (format_datetime(NOW), 100, 99, "bad"),
        (format_datetime(NOW), float("nan"), 99, None),
    ],
)
def test_clock_rejects_bad_slow_or_cached_samples(header, received, sent, age):
    school_clock.observe(header, received, sent_at=sent, age_header=age)
    assert school_clock.now()[1] == "local"


def test_clock_ignores_late_older_request(monkeypatch):
    monkeypatch.setattr(school_clock.time, "monotonic", lambda: 102)
    school_clock.observe(format_datetime(NOW), 101, sent_at=100)
    school_clock.observe(format_datetime(NOW - timedelta(hours=1)), 102, sent_at=99)
    assert school_clock.now() == (NOW + timedelta(seconds=1), "school")


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-24 12:00:00",
        "2026/09/24 12:00",
        "2026-09-24T04:00:00Z",
        NOW.timestamp(),
        int(NOW.timestamp() * 1000),
        str(int(NOW.timestamp() * 1000)),
    ],
)
def test_school_time_formats(value):
    assert logic._parse_school_start_time(value) == NOW


@pytest.mark.parametrize(
    "value", [None, "", True, "2026-09-24", float("nan"), float("inf"), -1, 10**50, {}, []]
)
def test_school_time_rejects_incomplete_or_invalid(value):
    with pytest.raises(logic.SchoolStartTimeUnavailableError):
        logic._parse_school_start_time(value)


class Response:
    status_code = 200
    text = "school batch"
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("school error")


def test_school_endpoint_is_read_only_and_matches_batch(monkeypatch):
    calls = []
    payload = {
        "code": "1",
        "dataList": [
            {"code": "old", "beginTime": "2026-09-23 12:00:00"},
            {"electiveBatch": {"code": "B1"}, "beginTime": "2026-09-24 12:00:00"},
        ],
    }
    monkeypatch.setattr(
        logic, "_school_request", lambda *a, **kw: calls.append((a, kw)) or Response(payload)
    )
    assert logic.fetch_undergraduate_start_time("B1", "token", "cookie") == NOW
    args, kwargs = calls[0]
    assert args[0] == "POST" and args[1].startswith("elective/batch.do?timestamp=")
    assert kwargs["read_only"] is True and kwargs["preference"] == config.BACKEND_PRIMARY


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "2", "dataList": [{"code": "B1", "beginTime": "2026-09-24 12:00:00"}]},
        {"code": True, "dataList": []},
        {"dataList": []},
        [],
        {"dataList": [{"beginTime": "2026-09-24 12:00:00"}]},
        {"dataList": [{"code": "B1", "beginTime": "2026-09-24 12:00:00"}] * 2},
        {"dataList": [{"code": "B1", "batchCode": "B2", "beginTime": "2026-09-24 12:00:00"}]},
        ValueError("not JSON"),
    ],
)
def test_school_endpoint_rejects_ambiguous_or_failed_response(monkeypatch, payload):
    monkeypatch.setattr(logic, "_school_request", lambda *a, **kw: Response(payload))
    with pytest.raises(logic.SchoolStartTimeUnavailableError):
        logic.fetch_undergraduate_start_time("B1", "token", "cookie")


def test_school_endpoint_detects_expiry(monkeypatch):
    response = Response(ValueError("HTML"))
    response.status_code = 401
    monkeypatch.setattr(logic, "_school_request", lambda *a, **kw: response)
    with pytest.raises(logic.SchoolBatchSessionExpiredError):
        logic.fetch_undergraduate_start_time("B1", "token", "cookie")


def test_appointment_creation_and_cancel_before_window(session):
    client, _, launches = session
    response = client.post("/api/enroll/schedule", json=appointment())
    assert response.status_code == 200
    state = response.json()
    assert state["status"] == "armed" and state["batch_code"] == "B1"
    assert app._has_session_work()
    assert launches == []
    assert (
        client.post("/api/enroll/schedule/cancel", json=cancellation(state["revision"])).json()[
            "status"
        ]
        == "idle"
    )


@pytest.mark.parametrize("phase", ["预选", "未开放", "已结束", "未知阶段", ""])
def test_nonautomatic_phase_cannot_arm(session, monkeypatch, phase):
    client, _, launches = session
    monkeypatch.setattr(config, "elective_batch_name", phase)
    assert client.post("/api/enroll/schedule", json=appointment()).status_code == 409
    assert launches == [] and not scheduled_enroll.is_active()


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_revision", True),
        ("expected_revision", "1"),
        ("batch_code", ""),
        ("confirmed_phase", "yes"),
    ],
)
def test_schedule_request_strict_types(session, field, value):
    client, _, _ = session
    assert (
        client.post("/api/enroll/schedule", json=appointment(**{field: value})).status_code == 422
    )


def test_revision_prevents_stale_tab_replacement_and_cancel(session):
    client, _, _ = session
    original = appointment()
    state = client.post("/api/enroll/schedule", json=original).json()
    stale = client.post("/api/enroll/schedule", json=original)
    assert stale.status_code == 409 and stale.json()["schedule"]["revision"] == state["revision"]
    assert (
        client.post(
            "/api/enroll/schedule/cancel", json=cancellation(original["expected_revision"])
        ).status_code
        == 409
    )
    assert scheduled_enroll.is_active()


def test_previous_process_cannot_replace_or_cancel_appointment(session):
    client, _, _ = session
    state = client.post("/api/enroll/schedule", json=appointment()).json()
    stale_instance = "0" * 32
    assert (
        client.post(
            "/api/enroll/schedule", json=appointment(expected_instance=stale_instance)
        ).status_code
        == 409
    )
    response = client.post(
        "/api/enroll/schedule/cancel", json=cancellation(instance=stale_instance)
    )
    assert response.status_code == 409
    assert "重新启动" in response.json()["message"]
    assert scheduled_enroll.status()["revision"] == state["revision"]
    assert scheduled_enroll.is_active()


def test_snapshot_sequence_increases_without_invalidating_form_revision():
    first, second = scheduled_enroll.status(), scheduled_enroll.status()
    assert second["snapshot_sequence"] > first["snapshot_sequence"]
    assert second["revision"] == first["revision"]
    assert second["instance_id"] == first["instance_id"]


def test_custom_appointment_also_binds_batch(session, monkeypatch):
    client, _, launches = session
    monkeypatch.setattr(
        app, "refresh_elective_batch", lambda *_: setattr(config, "elective_batch_code", "B2")
    )
    assert client.post("/api/enroll/schedule", json=appointment()).status_code == 409
    assert launches == []


def test_school_time_requires_reconfirmation(session, monkeypatch):
    client, _, _ = session
    target = datetime.now(UTC) + timedelta(minutes=10)
    monkeypatch.setattr(logic, "fetch_undergraduate_start_time", lambda *_: target)
    school_time = client.get("/api/enroll/school-start-time").json()
    changed = client.post(
        "/api/enroll/schedule",
        json=appointment(mode="school", expected_target_at=beijing(target + timedelta(minutes=1))),
    )
    assert (
        changed.status_code == 409 and changed.json()["error_code"] == "SCHOOL_START_TIME_CHANGED"
    )
    armed = client.post(
        "/api/enroll/schedule",
        json=appointment(mode="school", expected_target_at=school_time["target_at"]),
    )
    assert armed.status_code == 200 and armed.json()["mode"] == "school"


@pytest.mark.parametrize("change", ["logout", "empty", "running", "cancel"])
def test_creation_rechecks_after_network_read(session, monkeypatch, change):
    client, rows, launches = session

    def refresh(*_):
        if change == "logout":
            auth_service.clear_login_state()
        elif change == "empty":
            rows.clear()
        elif change == "running":
            monkeypatch.setattr(app, "is_enroll_task_running", lambda: True)
        else:
            scheduled_enroll.cancel()

    monkeypatch.setattr(app, "refresh_elective_batch", refresh)
    assert client.post("/api/enroll/schedule", json=appointment()).status_code == 409
    assert not scheduled_enroll.is_active() and launches == []


def test_graduate_apis_rejected(session, monkeypatch):
    client, _, _ = session
    monkeypatch.setattr(app, "is_graduate", lambda: True)
    assert client.get("/api/enroll/school-start-time").status_code == 400
    assert client.get("/api/enroll/schedule").status_code == 400
    assert client.post("/api/enroll/schedule", json=appointment()).status_code == 400
    assert client.post("/api/enroll/schedule/cancel", json=cancellation(1)).status_code == 400


def test_due_uses_guarded_start_exactly_once(session):
    _, _, launches = session
    identity = app._session_identity(app.get_session_snapshot())

    def start(generation):
        result = asyncio.run(
            app._start_enrollment(
                app.EnrollmentStartRequest(confirmed_phase=True),
                "B1",
                expected_account=identity,
                scheduled_generation=generation,
            )
        )
        return not result.is_error, result.message

    scheduled_enroll.arm(datetime.now(UTC), identity[0], start)
    wait_status("started")
    assert launches == [True]


@pytest.mark.parametrize("action", ["cancel", "logout_login", "shutdown", "manual_start"])
def test_cancel_during_start_verification_prevents_worker(session, monkeypatch, action):
    client, _, launches = session
    entered, release = threading.Event(), threading.Event()
    identity = app._session_identity(app.get_session_snapshot())

    def refresh(*_):
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(app, "refresh_elective_batch", refresh)

    def start(generation):
        result = asyncio.run(
            app._start_enrollment(
                app.EnrollmentStartRequest(confirmed_phase=True),
                "B1",
                expected_account=identity,
                scheduled_generation=generation,
            )
        )
        return (False, "cancelled") if hasattr(result, "body") else (True, result.message)

    scheduled_enroll.arm(datetime.now(UTC), identity[0], start)
    try:
        assert entered.wait(2)
        if action == "cancel":
            response = client.post(
                "/api/enroll/schedule/cancel",
                json=cancellation(),
            )
            assert response.status_code == 200
        elif action == "logout_login":
            auth_service.clear_login_state()
            auth_service.save_login_state("new", "captcha", identity[0], "test-only", "new-token")
            assert app._session_identity(app.get_session_snapshot()) != identity
        elif action == "shutdown":
            scheduled_enroll.shutdown()
        else:
            assert scheduled_enroll.commit_start(None, lambda: True)
    finally:
        release.set()
    for thread in threading.enumerate():
        if thread.name == "scheduled-enrollment":
            thread.join(2)
    assert launches == [] and scheduled_enroll.status()["status"] == "idle"


def test_old_account_cannot_start_recovery(session, monkeypatch):
    identity = app._session_identity(app.get_session_snapshot())
    auth_service.clear_login_state()
    auth_service.save_login_state("new", "captcha", identity[0], "test-only", "new-token")
    called = []
    monkeypatch.setattr(logic, "verify_vcode_login_flow", lambda **kw: called.append(True))
    assert not auth_service.attempt_automatic_relogin(expected_account=identity)[0]
    assert called == []


def test_missing_session_is_recovered_with_existing_budget(session, monkeypatch):
    identity = app._session_identity(app.get_session_snapshot())
    monkeypatch.setattr(config, "token", "")
    calls = []

    def recover(max_attempts, *, expected_account):
        calls.append((max_attempts, expected_account))
        config.token = "restored"
        return True, ""

    monkeypatch.setattr(app, "attempt_automatic_relogin", recover)
    assert app._refresh_schedule_context(identity, "B1")["logged_in"]
    assert calls == [(config.ocr_relogin_max_attempts, identity)]


def test_expired_school_time_read_recovers_once(session, monkeypatch):
    calls = []

    def read(*_):
        calls.append(True)
        if len(calls) == 1:
            raise logic.SchoolBatchSessionExpiredError("expired")
        return NOW

    monkeypatch.setattr(logic, "fetch_undergraduate_start_time", read)
    recovered = []
    monkeypatch.setattr(
        app, "attempt_automatic_relogin", lambda *a, **kw: recovered.append(kw) or (True, "")
    )
    assert app._read_school_start_time(app._session_identity(app.get_session_snapshot()), "B1")[
        "target_at"
    ] == beijing(NOW)
    assert len(calls) == 2 and len(recovered) == 1


def test_recovery_cannot_switch_the_appointment_batch(session, monkeypatch):
    identity = app._session_identity(app.get_session_snapshot())
    monkeypatch.setattr(config, "token", "")

    def recover(*_, **kwargs):
        config.token = "restored"
        config.elective_batch_code = "B2"
        return True, ""

    monkeypatch.setattr(app, "attempt_automatic_relogin", recover)
    with pytest.raises(scheduled_enroll.ScheduleInvalidatedError, match="批次"):
        app._refresh_schedule_context(identity, "B1")


def test_due_queue_or_phase_change_does_not_launch(session, monkeypatch):
    _, rows, launches = session
    identity = app._session_identity(app.get_session_snapshot())
    rows[0]["auto_enabled"] = 0
    result = asyncio.run(
        app._start_enrollment(
            app.EnrollmentStartRequest(confirmed_phase=True), "B1", expected_account=identity
        )
    )
    assert result.status_code == 400
    rows[0]["auto_enabled"] = 1
    monkeypatch.setattr(config, "elective_batch_name", "预选")
    result = asyncio.run(
        app._start_enrollment(
            app.EnrollmentStartRequest(confirmed_phase=True), "B1", expected_account=identity
        )
    )
    assert result.status_code == 409 and launches == []


def test_start_time_read_discards_changed_credentials(session, monkeypatch):
    def read(*_):
        config.token = "another-session"
        return NOW

    monkeypatch.setattr(logic, "fetch_undergraduate_start_time", read)
    with pytest.raises(logic.SchoolStartTimeUnavailableError, match="会话已更新"):
        app._read_school_start_time(app._session_identity(app.get_session_snapshot()), "B1")


@pytest.mark.parametrize("failure", ["network", "account", "missing_time"])
def test_due_verification_failure_does_not_start(failure):
    calls = []

    def sync():
        if failure == "network":
            raise requests.Timeout("offline")
        if failure == "account":
            raise scheduled_enroll.ScheduleInvalidatedError("账号已变化")
        return None

    scheduled_enroll.arm(
        datetime.now(UTC),
        "student",
        lambda _: (calls.append(True) or True, "started"),
        sync,
        mode="school",
    )
    wait_status("failed")
    assert calls == []


def test_due_time_postponement_waits_for_new_target(monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(school_clock, "now", lambda: (clock["now"], "school"))
    calls = []
    revised = NOW + timedelta(minutes=1)
    scheduled_enroll.arm(
        NOW,
        "student",
        lambda _: (calls.append(True) or True, "started"),
        lambda: revised,
        mode="school",
    )
    state = wait_status("armed")
    deadline = time.monotonic() + 2
    while state["target_at"] != beijing(revised) and time.monotonic() < deadline:
        time.sleep(0.01)
        state = scheduled_enroll.status()
    assert state["target_at"] == beijing(revised) and calls == []
    clock["now"] = revised
    wait_status("started")
    assert calls == [True]


def test_missed_time_and_very_distant_revision_never_start(monkeypatch):
    monkeypatch.setattr(school_clock, "now", lambda: (NOW, "school"))
    calls = []
    scheduled_enroll.arm(
        NOW - timedelta(minutes=3), "student", lambda _: (calls.append(True) or True, "started")
    )
    assert "错过" in wait_status("failed")["message"]
    scheduled_enroll.arm(
        NOW,
        "student",
        lambda _: (calls.append(True) or True, "started"),
        lambda: NOW + timedelta(days=8),
        mode="school",
    )
    assert "7 天" in wait_status("failed")["message"]
    assert calls == []


def test_periodic_sync_error_is_visible_and_cancel_still_works():
    def sync():
        raise requests.Timeout("offline")

    scheduled_enroll.arm(
        datetime.now(UTC) + timedelta(seconds=60), "student", lambda _: (True, "started"), sync
    )
    deadline = time.monotonic() + 2
    while not scheduled_enroll.status()["sync_message"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "网络" in scheduled_enroll.status()["sync_message"]
    assert scheduled_enroll.cancel()


def test_failed_thread_start_reports_failure(monkeypatch):
    def fail(_self):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError):
        scheduled_enroll.arm(NOW, "student", lambda _: (True, "started"))
    assert scheduled_enroll.status()["status"] == "failed"
