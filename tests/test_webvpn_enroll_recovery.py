"""An expired gateway pauses every enrollment mode without replaying a write."""

from __future__ import annotations

import copy
import json
from urllib.parse import urlsplit

import pytest
import requests

import config
import database
import study_program
from database import DatabaseManager
from services import cart_service, course_service, enroll_service


@pytest.fixture
def webvpn_task(monkeypatch, tmp_path):
    monkeypatch.setenv(study_program.PROGRAM_ENV, study_program.UNDERGRADUATE)
    for name, value in {
        "backend_preference": config.BACKEND_WEBVPN,
        "active_backend": config.BACKEND_WEBVPN,
        "webvpn_cookie": "_webvpn_key=test; webvpn_username=test; webvpn_username_NS_Sig=test",
        "combined_cookie": "route=test; JSESSIONID=test",
        "token": "test-token",
        "student_id": "2024110122",
        "elective_batch_code": "2025202601",
        "campus_code": "01",
        "count": 3,
    }.items():
        monkeypatch.setattr(config, name, value)
    for name in ("_task_state", "_progress"):
        monkeypatch.setattr(enroll_service, name, copy.deepcopy(getattr(enroll_service, name)))
    for name in ("_business_failure_counts", "_unknown_streak_counts", "_network_streak_counts"):
        monkeypatch.setattr(enroll_service, name, {})
    monkeypatch.setattr(course_service, "pace_catalog_request", lambda: 0)
    monkeypatch.setattr(
        enroll_service,
        "_wait_between_requests",
        lambda *_args: pytest.fail("Gateway expiry must pause before another retry"),
    )
    monkeypatch.setattr(
        enroll_service,
        "attempt_automatic_relogin",
        lambda *_args, **_kwargs: pytest.fail("School OCR cannot restore WebVPN authentication"),
    )
    db = DatabaseManager(str(tmp_path / "gateway-expiry.db"))
    monkeypatch.setattr(cart_service, "db", db)
    course = enroll_service.EnrollmentCourse(
        id="202520262010192004801", type="FANKC", name="测试课"
    )
    assert cart_service.add_course(course)["success"]
    assert cart_service.update_status(course.id, database.STATUS_IN_PROGRESS)
    enroll_service._release_enroll_task()
    assert enroll_service.reserve_enroll_task()
    enroll_service._reset_progress([course])
    try:
        yield course, db
    finally:
        enroll_service._release_enroll_task()


@pytest.mark.parametrize(
    ("stage", "expected_paths"),
    [
        ("scan", ["elective/programCourse.do"]),
        ("submission", ["elective/volunteer.do"]),
        ("confirmation", ["elective/volunteer.do", "elective/courseResult.do"]),
    ],
)
def test_gateway_expiry_pauses_with_manual_reauthentication_instructions(
    monkeypatch, webvpn_task, stage, expected_paths
):
    course, db = webvpn_task
    calls = []

    def request(_session, method, url, **kwargs):
        assert method.upper() == "POST"
        assert urlsplit(url).netloc == "bkxk.webvpn.szu.edu.cn"
        assert kwargs["allow_redirects"] is False
        path = urlsplit(url).path.removeprefix("/xsxkapp/sys/xsxkapp/")
        calls.append(path)
        assert len(calls) <= len(expected_paths), "Do not replay a request after gateway expiry"
        response = requests.Response()
        response.url = url
        if stage == "confirmation" and len(calls) == 1:
            response.status_code = 200
            response._content = json.dumps({"code": "1", "msg": "添加选课志愿成功"}).encode()
        else:
            response.status_code = 302
            response.headers["Location"] = "https://webvpn.szu.edu.cn/users/sign_in"
            response._content = b""
        return response

    monkeypatch.setattr(requests.sessions.Session, "request", request)
    if stage == "scan":
        enroll_service.set_enroll_mode("scan")

    outcome = enroll_service.grab_courses([course])

    assert outcome == enroll_service.GrabOutcome.PAUSED
    assert calls == expected_paths
    assert config.webvpn_cookie == ""
    state = enroll_service.get_enroll_task_state()
    assert state["paused"] is True
    assert state["pause_source"] == "webvpn_auth_required"
    for phrase in ("WebVPN", "统一认证", "已过期", "停止任务", "退出登录", "登录页"):
        assert phrase in state["pause_reason"]
    assert db.get_courses_by_status(database.STATUS_SUCCESS) == []
    assert db.get_courses_by_status(database.STATUS_FAILED) == []
    assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == course.id
    progress = enroll_service.get_enroll_progress()["courses"][0]
    assert progress["failures"] == 0
    assert progress["message"] == state["pause_reason"]
    # Even if a caller asks for another pass, the paused task sends nothing.
    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.PAUSED
    assert calls == expected_paths
