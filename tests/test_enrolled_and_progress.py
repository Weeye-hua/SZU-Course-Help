from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
import requests
from starlette.testclient import TestClient

import app
import config
import database
from database import DatabaseManager
from services import cart_service, course_service, enroll_service

client = TestClient(app.app)


class Resp:
    """轻量的伪学校响应，用于驱动抢课分类逻辑（不发真实请求）。"""

    def __init__(self, text, code="1", status=200):
        self.text = text
        self._code = code
        self.status_code = status

    def json(self):
        return {"code": self._code}


def _course(**overrides):
    values = {"id": "c1", "type": "FANKC", "name": "示例课程"}
    values.update(overrides)
    return SimpleNamespace(**values)


def _prime_cart(monkeypatch, tmp_path, courses, status=database.STATUS_IN_PROGRESS):
    db = DatabaseManager(str(tmp_path / "grab.db"))
    monkeypatch.setattr(cart_service, "db", db)
    for course in courses:
        cart_service.add_course(course)
        cart_service.update_status(course.id, status)
    monkeypatch.setattr(config, "count", 3)
    monkeypatch.setattr(config, "delay", 0)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "query_enrolled_courses",
        lambda *_args, **_kwargs: [{"teachingClassID": course.id} for course in courses],
    )
    monkeypatch.setattr(enroll_service, "ENROLLMENT_CONFIRM_RETRY_SECONDS", 0)
    return db


# ------------------------------------------------------------------
# 已选课程服务与接口
# ------------------------------------------------------------------


def test_get_enrolled_courses_maps_school_rows(monkeypatch):
    monkeypatch.setattr(config, "token", "t")
    monkeypatch.setattr(config, "combined_cookie", "c")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(
        course_service.choose_course,
        "query_enrolled_courses",
        lambda cookie, token, verbose=True: [
            {
                "courseName": "数据库系统",
                "teacherName": "陈老师",
                "teachingPlace": "H100",
                "credit": "3",
                "teachingClassID": "tc-1",
                "courseTypeName": "方案内课程",
            }
        ],
    )

    ok, data = course_service.get_enrolled_courses()
    assert ok
    assert data[0]["course_name"] == "数据库系统"
    assert data[0]["teacher_name"] == "陈老师"
    assert data[0]["teaching_class_id"] == "tc-1"
    assert data[0]["credit"] == "3"


def test_get_enrolled_courses_requires_session(monkeypatch):
    monkeypatch.setattr(config, "token", "")
    monkeypatch.setattr(config, "combined_cookie", "")
    ok, data = course_service.get_enrolled_courses()
    assert not ok and data == course_service.SESSION_EXPIRED


def test_get_enrolled_courses_detects_school_login_page(monkeypatch):
    monkeypatch.setattr(config, "token", "expired")
    monkeypatch.setattr(config, "combined_cookie", "expired")
    monkeypatch.setattr(
        course_service.choose_course,
        "query_enrolled_courses",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            course_service.choose_course.SchoolSessionExpiredError()
        ),
    )

    ok, data = course_service.get_enrolled_courses()
    assert not ok and data == course_service.SESSION_EXPIRED


def test_enrolled_endpoint_requires_login(monkeypatch):
    monkeypatch.setattr(config, "token", "")
    monkeypatch.setattr(config, "combined_cookie", "")
    assert client.get("/api/school/enrolled").status_code == 401


def test_enrolled_endpoint_returns_courses(monkeypatch):
    monkeypatch.setattr(config, "token", "t")
    monkeypatch.setattr(config, "combined_cookie", "c")
    monkeypatch.setattr(
        app,
        "get_enrolled_courses",
        lambda: (True, [{"course_name": "算法", "teaching_class_id": "1"}]),
    )
    response = client.get("/api/school/enrolled")
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["courses"][0]["course_name"] == "算法"


def test_mode_endpoint_returns_complete_settings_snapshot(monkeypatch):
    monkeypatch.setitem(enroll_service._settings, "boost_interval_ms", 1200)
    monkeypatch.setitem(enroll_service._settings, "normal_interval_ms", 12300)
    monkeypatch.setitem(enroll_service._settings, "scan_interval_ms", 65000)
    monkeypatch.setitem(enroll_service._settings, "boost_failure_limit", 7)
    monkeypatch.setitem(enroll_service._settings, "normal_failure_limit", None)

    response = client.post("/api/enroll/mode", json={"mode": "normal"})

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "normal"
    assert body["settings"] == {
        "boost_interval_ms": 1200,
        "normal_interval_ms": 12300,
        "scan_interval_ms": 65000,
        "boost_failure_limit": 7,
        "normal_failure_limit": None,
        "mode": "normal",
    }
    enroll_service._release_enroll_task()


def test_failure_limit_api_accepts_finite_and_unlimited_values(monkeypatch):
    monkeypatch.setitem(enroll_service._settings, "boost_failure_limit", 5)
    monkeypatch.setitem(enroll_service._settings, "normal_failure_limit", 10)

    response = client.patch(
        "/api/enroll/settings",
        json={"boost_failure_limit": 37, "normal_failure_limit": None},
    )

    assert response.status_code == 200
    assert response.json()["boost_failure_limit"] == 37
    assert response.json()["normal_failure_limit"] is None
    settings = client.get("/api/enroll/settings").json()
    assert settings["boost_failure_limit"] == 37
    assert settings["normal_failure_limit"] is None


@pytest.mark.parametrize("value", [0, -1, 1.5, "12", True, 1_000_001])
def test_failure_limit_api_rejects_invalid_values(value):
    response = client.patch(
        "/api/enroll/settings",
        json={"boost_failure_limit": value},
    )

    assert response.status_code == 422


# ------------------------------------------------------------------
# 抢课分类逻辑
# ------------------------------------------------------------------


def test_success_marks_course_and_stops_requesting_it(tmp_path, monkeypatch):
    course = _course(id="ok1", name="成功课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    calls = []
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda cid, ctype, campus: calls.append(cid) or Resp("添加选课志愿成功"),
    )
    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.COMPLETED
    assert db.get_courses_by_status(database.STATUS_SUCCESS)[0]["id"] == "ok1"
    # 成功后不再对该课程发请求（config.count=3 也只调用一次）
    assert calls == ["ok1"]


def test_success_is_not_recorded_until_exact_class_id_appears(tmp_path, monkeypatch):
    course = _course(id="strict-id", name="严格核验课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    confirmation_calls = []
    snapshots = [
        [{"teachingClassID": "other-id", "courseName": course.name}],
        [{"teachingClassId": course.id, "courseName": course.name}],
    ]
    monkeypatch.setattr(config, "count", 1)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("添加选课志愿成功"),
    )

    def query_selected(*_args, **kwargs):
        confirmation_calls.append(kwargs["timeout"])
        return snapshots.pop(0)

    monkeypatch.setattr(
        enroll_service.choose_course,
        "query_enrolled_courses",
        query_selected,
    )

    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.COMPLETED
    assert confirmation_calls == [
        enroll_service.ENROLLMENT_CONFIRM_TIMEOUT,
        enroll_service.ENROLLMENT_CONFIRM_TIMEOUT,
    ]
    assert db.get_courses_by_status(database.STATUS_SUCCESS)[0]["id"] == course.id


def test_false_success_stays_retryable_when_selected_list_is_valid_but_absent(
    tmp_path, monkeypatch
):
    course = _course(id="false-success", name="学校假成功课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    enroll_service._reset_progress([course])
    monkeypatch.setattr(config, "count", 1)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("添加选课志愿成功"),
    )
    monkeypatch.setattr(
        enroll_service.choose_course,
        "query_enrolled_courses",
        lambda *_args, **_kwargs: [],
    )

    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.CONTINUE
    assert db.get_courses_by_status(database.STATUS_SUCCESS) == []
    assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == course.id
    progress = enroll_service.get_enroll_progress()["courses"][0]
    assert progress["failures"] == 1
    assert "尚未出现该教学班" in progress["message"]


def test_already_selected_response_requires_and_accepts_exact_confirmation(tmp_path, monkeypatch):
    course = _course(id="already-selected", name="已选提示课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setattr(config, "count", 1)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("该教学班已经选过", code="0"),
    )

    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.COMPLETED
    assert db.get_courses_by_status(database.STATUS_SUCCESS)[0]["id"] == course.id
    assert db.get_courses_by_status(database.STATUS_FAILED) == []


def test_confirmation_session_expiry_requests_relogin(tmp_path, monkeypatch):
    course = _course(id="confirm-expired", name="核验过期课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setattr(config, "count", 1)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("添加选课志愿成功"),
    )
    monkeypatch.setattr(
        enroll_service.choose_course,
        "query_enrolled_courses",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            enroll_service.choose_course.SchoolSessionExpiredError("expired")
        ),
    )

    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.SESSION_EXPIRED
    assert db.get_courses_by_status(database.STATUS_SUCCESS) == []
    assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == course.id


@pytest.mark.parametrize(
    "selected_rows",
    [
        [{"courseName": "缺失教学班 ID"}],
        ["not-an-object"],
    ],
)
def test_unverifiable_selected_list_pauses_without_claiming_success(
    tmp_path, monkeypatch, selected_rows
):
    course = _course(id="unverifiable", name="无法核验课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setattr(config, "count", 1)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("添加选课志愿成功"),
    )
    monkeypatch.setattr(
        enroll_service.choose_course,
        "query_enrolled_courses",
        lambda *_args, **_kwargs: selected_rows,
    )

    assert enroll_service.reserve_enroll_task()
    try:
        enroll_service._reset_progress([course])
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.PAUSED
        state = enroll_service.get_enroll_task_state()
        assert state["pause_source"] == "confirmation_unavailable"
        assert db.get_courses_by_status(database.STATUS_SUCCESS) == []
        assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == course.id
    finally:
        enroll_service._release_enroll_task()
        enroll_service._set_progress_finished()


def test_confirmation_network_failure_pauses_without_resubmitting(tmp_path, monkeypatch):
    course = _course(id="confirm-network", name="核验网络异常课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    submissions = []
    confirmation_calls = []
    monkeypatch.setattr(config, "count", 5)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: submissions.append(1) or Resp("添加选课志愿成功"),
    )

    def unavailable(*_args, **_kwargs):
        confirmation_calls.append(1)
        raise requests.Timeout("selected-course endpoint timed out")

    monkeypatch.setattr(
        enroll_service.choose_course,
        "query_enrolled_courses",
        unavailable,
    )

    assert enroll_service.reserve_enroll_task()
    try:
        enroll_service._reset_progress([course])
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.PAUSED
        assert submissions == [1]
        assert len(confirmation_calls) == enroll_service.ENROLLMENT_CONFIRM_ATTEMPTS
        assert db.get_courses_by_status(database.STATUS_SUCCESS) == []
        assert enroll_service.get_enroll_task_state()["pause_source"] == (
            "confirmation_unavailable"
        )
    finally:
        enroll_service._release_enroll_task()
        enroll_service._set_progress_finished()


def test_stale_absent_snapshot_followed_by_failures_is_not_treated_as_current(
    tmp_path, monkeypatch
):
    course = _course(id="stale-confirmation", name="核验快照过期课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    responses = iter([[], requests.Timeout("late timeout"), requests.Timeout("late timeout")])
    monkeypatch.setattr(config, "count", 1)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("添加选课志愿成功"),
    )

    def mixed_confirmation(*_args, **_kwargs):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        enroll_service.choose_course,
        "query_enrolled_courses",
        mixed_confirmation,
    )

    assert enroll_service.reserve_enroll_task()
    try:
        enroll_service._reset_progress([course])
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.PAUSED
        assert db.get_courses_by_status(database.STATUS_SUCCESS) == []
        assert enroll_service.get_enroll_task_state()["pause_source"] == (
            "confirmation_unavailable"
        )
    finally:
        enroll_service._release_enroll_task()
        enroll_service._set_progress_finished()


def test_confirmed_course_pauses_if_local_success_state_cannot_be_saved(tmp_path, monkeypatch):
    course = _course(id="local-write", name="本地写入异常课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setattr(config, "count", 1)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("添加选课志愿成功"),
    )
    monkeypatch.setattr(enroll_service.cart_service, "update_status", lambda *_args: False)

    assert enroll_service.reserve_enroll_task()
    try:
        enroll_service._reset_progress([course])
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.PAUSED
        assert enroll_service.get_enroll_task_state()["pause_source"] == "local_state_error"
        assert db.get_courses_by_status(database.STATUS_SUCCESS) == []
        assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == course.id
    finally:
        enroll_service._release_enroll_task()
        enroll_service._set_progress_finished()


def test_user_pause_interrupts_confirmation_retry(tmp_path, monkeypatch):
    course = _course(id="confirm-pause", name="核验中暂停课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setattr(config, "count", 1)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("添加选课志愿成功"),
    )
    monkeypatch.setattr(
        enroll_service.choose_course,
        "query_enrolled_courses",
        lambda *_args, **_kwargs: [],
    )

    def pause_at_retry(_seconds):
        assert enroll_service.pause_enroll_task()[0]
        return False

    assert enroll_service.reserve_enroll_task()
    try:
        enroll_service._reset_progress([course])
        monkeypatch.setattr(enroll_service, "_wait_between_requests", pause_at_retry)
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.PAUSED
        assert db.get_courses_by_status(database.STATUS_SUCCESS) == []
        assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == course.id
        assert enroll_service.get_enroll_task_state()["pause_source"] == "user"
    finally:
        enroll_service._release_enroll_task()
        enroll_service._set_progress_finished()


def test_terminal_error_marks_failed_and_stops(tmp_path, monkeypatch):
    course = _course(id="bad1", name="冲突课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    calls = []
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda cid, ctype, campus: calls.append(cid) or Resp("上课时间冲突", code="0"),
    )
    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.COMPLETED
    assert db.get_courses_by_status(database.STATUS_FAILED)[0]["id"] == "bad1"
    assert calls == ["bad1"]


def test_capacity_full_keeps_retrying(tmp_path, monkeypatch):
    course = _course(id="full1", name="满员课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    calls = []
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda cid, ctype, campus: calls.append(cid) or Resp("该课程超过课容量", code="0"),
    )
    monkeypatch.setattr(config, "count", 25)
    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.CONTINUE
    # 满员是正常可重试结果，不受旧版 20 次未知返回阈值限制。
    assert len(calls) == 25
    assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == "full1"


def test_enroll_defaults_and_business_failures_downgrade_modes(tmp_path, monkeypatch):
    course = _course(id="mode1", name="模式课")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setattr(config, "count", 5)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("该课程超过课容量", code="0"),
    )
    assert enroll_service.get_enroll_settings()["boost_interval_ms"] == 1000
    assert enroll_service.get_enroll_settings()["normal_interval_ms"] == 10000
    assert enroll_service.get_enroll_settings()["scan_interval_ms"] == 60000
    assert enroll_service.get_enroll_settings()["boost_failure_limit"] == 5
    assert enroll_service.get_enroll_settings()["normal_failure_limit"] == 10

    assert enroll_service.reserve_enroll_task()
    try:
        enroll_service._reset_progress([course])
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.CONTINUE
        assert enroll_service.get_enroll_task_state()["mode"] == "normal"
        monkeypatch.setattr(config, "count", 10)
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.CONTINUE
        assert enroll_service.get_enroll_task_state()["mode"] == "scan"
        row = db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]
        assert row["id"] == "mode1"
    finally:
        enroll_service._release_enroll_task()


def test_custom_failure_limits_control_each_mode_downgrade(tmp_path, monkeypatch):
    course = _course(id="custom-mode", name="自定义阈值课程")
    _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setitem(enroll_service._settings, "boost_failure_limit", 2)
    monkeypatch.setitem(enroll_service._settings, "normal_failure_limit", 4)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("该课程超过课容量", code="0"),
    )

    assert enroll_service.reserve_enroll_task()
    try:
        enroll_service._reset_progress([course])
        monkeypatch.setattr(config, "count", 2)
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.CONTINUE
        assert enroll_service.get_enroll_task_state()["mode"] == "normal"

        monkeypatch.setattr(config, "count", 2)
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.CONTINUE
        assert enroll_service.get_enroll_task_state()["mode"] == "scan"
    finally:
        enroll_service._release_enroll_task()


def test_unlimited_boost_failure_limit_never_auto_downgrades(tmp_path, monkeypatch):
    course = _course(id="unlimited-boost", name="爆发无限阈值课程")
    _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setitem(enroll_service._settings, "boost_failure_limit", None)
    monkeypatch.setitem(enroll_service._settings, "normal_failure_limit", 10)
    monkeypatch.setattr(config, "count", 25)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("该课程超过课容量", code="0"),
    )

    assert enroll_service.reserve_enroll_task()
    try:
        enroll_service._reset_progress([course])
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.CONTINUE
        assert enroll_service.get_enroll_task_state()["mode"] == "boost"
        progress = enroll_service.get_enroll_progress()["courses"][0]
        assert progress["failures"] == 25
    finally:
        enroll_service._release_enroll_task()


def test_unlimited_normal_failure_limit_never_auto_downgrades(tmp_path, monkeypatch):
    course = _course(id="unlimited-normal", name="一般无限阈值课程")
    _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setitem(enroll_service._settings, "boost_failure_limit", 2)
    monkeypatch.setitem(enroll_service._settings, "normal_failure_limit", None)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: Resp("该课程超过课容量", code="0"),
    )

    assert enroll_service.reserve_enroll_task()
    try:
        enroll_service._reset_progress([course])
        monkeypatch.setattr(config, "count", 2)
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.CONTINUE
        assert enroll_service.get_enroll_task_state()["mode"] == "normal"

        monkeypatch.setattr(config, "count", 25)
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.CONTINUE
        assert enroll_service.get_enroll_task_state()["mode"] == "normal"
        progress = enroll_service.get_enroll_progress()["courses"][0]
        assert progress["failures"] == 27
    finally:
        enroll_service._release_enroll_task()


def test_transient_rate_limit_takes_priority_over_broad_terminal_wording(tmp_path, monkeypatch):
    course = _course(id="busy1", name="频控课程")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    calls = []
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: calls.append(1) or Resp("当前系统不允许频繁操作，请稍后再试", code="2"),
    )

    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.CONTINUE
    assert len(calls) == 3
    assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == "busy1"
    assert db.get_courses_by_status(database.STATUS_FAILED) == []


def test_session_expired_returns_false_for_relogin(tmp_path, monkeypatch):
    course = _course(id="exp1", name="过期课")
    _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda cid, ctype, campus: Resp("login required", code="302", status=401),
    )
    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.SESSION_EXPIRED


def test_http_200_login_page_triggers_relogin(tmp_path, monkeypatch):
    course = _course(id="html-expired", name="过期登录页")
    _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda cid, ctype, campus: Resp(
            '<form action="student/check/login.do"><input name="vtoken">'
            '<input name="loginPwd"></form>',
            code="0",
            status=200,
        ),
    )

    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.SESSION_EXPIRED


def test_unknown_response_does_not_starve_other_courses(tmp_path, monkeypatch):
    """核心回归：修复旧代码 break 导致的多课程互相饿死问题。"""
    a = _course(id="A", name="未知返回课")
    b = _course(id="B", name="成功课")
    db = _prime_cart(monkeypatch, tmp_path, [a, b])
    monkeypatch.setattr(config, "count", 1)  # 单轮

    def fake(cid, ctype, campus):
        if cid == "A":
            return Resp("系统繁忙，请稍后再试", code="0")
        return Resp("添加选课志愿成功")

    monkeypatch.setattr(enroll_service.choose_course, "submit_course_selection", fake)
    assert enroll_service.grab_courses([a, b]) == enroll_service.GrabOutcome.CONTINUE
    # 即便 A 返回未知，B 在同一轮也能拿到抢课机会并成功
    assert db.get_courses_by_status(database.STATUS_SUCCESS)[0]["id"] == "B"


def test_unknown_response_default_pause_threshold_is_two_thousand():
    assert config.DEFAULT_UNKNOWN_RESPONSE_PAUSE_THRESHOLD == 2000


def test_multi_course_one_succeeds_other_continues(tmp_path, monkeypatch):
    a = _course(id="A", name="A课")
    b = _course(id="B", name="B课")
    db = _prime_cart(monkeypatch, tmp_path, [a, b])
    calls = []

    def fake(cid, ctype, campus):
        calls.append(cid)
        if cid == "A":
            return Resp("添加选课志愿成功")
        return Resp("该课程超过课容量", code="0")

    monkeypatch.setattr(enroll_service.choose_course, "submit_course_selection", fake)
    assert enroll_service.grab_courses([a, b]) == enroll_service.GrabOutcome.CONTINUE
    assert db.get_courses_by_status(database.STATUS_SUCCESS)[0]["id"] == "A"
    assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == "B"
    assert calls.count("A") == 1  # 抢到后停止
    assert calls.count("B") == 3  # 未抢到持续尝试


def test_closed_school_window_pauses_after_one_request(tmp_path, monkeypatch):
    course = _course(id="closed1", name="未开放课程")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    calls = []
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: (
            calls.append(1)
            or Resp('{"data":null,"msg":"当前时间不在选课开放时间范围内","code":"2"}', code="2")
        ),
    )

    assert enroll_service.reserve_enroll_task()
    try:
        outcome = enroll_service.grab_courses([course])
        state = enroll_service.get_enroll_task_state()
        assert outcome == enroll_service.GrabOutcome.PAUSED
        assert calls == [1]
        assert state["paused"] is True
        assert state["pause_source"] == "school_window"
        assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == "closed1"
        assert db.get_courses_by_status(database.STATUS_FAILED) == []
    finally:
        enroll_service._release_enroll_task()


def test_unknown_response_threshold_pauses_without_failing(tmp_path, monkeypatch):
    course = _course(id="unknown1", name="未知响应课程")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    unknown_limit = 3
    monkeypatch.setattr(config, "unknown_response_pause_threshold", unknown_limit)
    monkeypatch.setattr(config, "count", unknown_limit + 1)
    calls = []
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: calls.append(1) or Resp("学校新增的未识别响应", code="9"),
    )

    assert enroll_service.reserve_enroll_task()
    try:
        outcome = enroll_service.grab_courses([course])
        assert outcome == enroll_service.GrabOutcome.PAUSED
        assert len(calls) == unknown_limit
        assert enroll_service.get_enroll_task_state()["pause_source"] == "unknown_response"
        assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == "unknown1"
        assert db.get_courses_by_status(database.STATUS_FAILED) == []
    finally:
        enroll_service._release_enroll_task()


def test_network_threshold_pauses_without_failing(tmp_path, monkeypatch):
    course = _course(id="network1", name="网络异常课程")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setattr(config, "count", enroll_service.MAX_NETWORK_STREAK + 1)
    calls = []

    def fail_request(*_args):
        calls.append(1)
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(enroll_service.choose_course, "submit_course_selection", fail_request)

    assert enroll_service.reserve_enroll_task()
    try:
        outcome = enroll_service.grab_courses([course])
        assert outcome == enroll_service.GrabOutcome.PAUSED
        assert len(calls) == enroll_service.MAX_NETWORK_STREAK
        assert enroll_service.get_enroll_task_state()["pause_source"] == "network_error"
        assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == "network1"
        assert db.get_courses_by_status(database.STATUS_FAILED) == []
    finally:
        enroll_service._release_enroll_task()


def test_internal_error_pauses_immediately_without_failing(tmp_path, monkeypatch):
    course = _course(id="internal1", name="内部异常课程")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    calls = []

    def fail_request(*_args):
        calls.append(1)
        raise ValueError("programming error")

    monkeypatch.setattr(enroll_service.choose_course, "submit_course_selection", fail_request)

    assert enroll_service.reserve_enroll_task()
    try:
        outcome = enroll_service.grab_courses([course])
        assert outcome == enroll_service.GrabOutcome.PAUSED
        assert calls == [1]
        assert enroll_service.get_enroll_task_state()["pause_source"] == "internal_error"
        assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == "internal1"
        assert db.get_courses_by_status(database.STATUS_FAILED) == []
    finally:
        enroll_service._release_enroll_task()


def test_user_pause_and_resume_preserve_queue(tmp_path, monkeypatch):
    course = _course(id="pause1", name="暂停课程")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    calls = []
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: calls.append(1) or Resp("添加选课志愿成功"),
    )

    assert enroll_service.reserve_enroll_task()
    try:
        assert enroll_service.pause_enroll_task()[0]
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.PAUSED
        assert calls == []
        assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == "pause1"

        assert enroll_service.resume_enroll_task()[0]
        assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.COMPLETED
        assert calls == [1]
        assert db.get_courses_by_status(database.STATUS_SUCCESS)[0]["id"] == "pause1"
    finally:
        enroll_service._release_enroll_task()


def test_paused_course_can_be_removed_only_after_worker_acknowledges_pause(tmp_path, monkeypatch):
    course = _course(id="remove-paused", name="暂停后移除课程")
    db = _prime_cart(monkeypatch, tmp_path, [course])
    enroll_service._reset_progress([course])

    assert enroll_service.reserve_enroll_task()
    waiter_result = []
    waiter = None
    try:
        assert enroll_service.pause_enroll_task()[0]
        state = enroll_service.get_enroll_task_state()
        assert state["paused"] is True
        assert state["pause_acknowledged"] is False

        blocked = enroll_service.remove_cart_course(course.id)
        assert blocked["success"] is False
        assert blocked["error_code"] == "ENROLL_TASK_PAUSE_PENDING"
        assert db.get_courses_by_status("")[0]["id"] == course.id

        waiter = threading.Thread(
            target=lambda: waiter_result.append(enroll_service._wait_until_resumed())
        )
        waiter.start()
        deadline = time.monotonic() + 2
        while (
            not enroll_service.get_enroll_task_state()["pause_acknowledged"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert enroll_service.get_enroll_task_state()["pause_acknowledged"] is True

        removed = enroll_service.remove_cart_course(course.id)
        assert removed["success"] is True
        assert removed["task_stopping"] is True
        assert db.get_courses_by_status("") == []
        assert enroll_service.get_enroll_progress()["courses"] == []
        assert enroll_service.get_enroll_task_state()["stopping"] is True

        waiter.join(timeout=2)
        assert not waiter.is_alive()
        assert waiter_result == [False]
    finally:
        enroll_service._release_enroll_task()
        enroll_service._set_progress_finished()
        if waiter is not None:
            waiter.join(timeout=2)


def test_pause_during_inflight_school_request_blocks_removal_until_response(tmp_path, monkeypatch):
    course = _course(id="inflight-remove", name="请求中的课程")
    db = _prime_cart(
        monkeypatch,
        tmp_path,
        [course],
        status=database.STATUS_NOT_STARTED,
    )
    request_started = threading.Event()
    release_response = threading.Event()

    def delayed_response(*_args):
        request_started.set()
        assert release_response.wait(timeout=2)
        return Resp("该课程超过课容量", code="0")

    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        delayed_response,
    )
    worker = threading.Thread(target=enroll_service.run_enroll_task)
    worker.start()
    try:
        assert request_started.wait(timeout=2)
        assert enroll_service.pause_enroll_task()[0]
        assert enroll_service.get_enroll_task_state()["pause_acknowledged"] is False

        blocked = enroll_service.remove_cart_course(course.id)
        assert blocked["error_code"] == "ENROLL_TASK_PAUSE_PENDING"
        assert db.get_courses_by_status("")[0]["id"] == course.id

        release_response.set()
        deadline = time.monotonic() + 2
        while (
            not enroll_service.get_enroll_task_state()["pause_acknowledged"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert enroll_service.get_enroll_task_state()["pause_acknowledged"] is True

        removed = enroll_service.remove_cart_course(course.id)
        assert removed["success"] is True
        assert removed["task_stopping"] is True
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert db.get_courses_by_status("") == []
    finally:
        release_response.set()
        enroll_service._release_enroll_task()
        worker.join(timeout=2)


def test_removing_one_course_from_paused_queue_preserves_remaining_work(tmp_path, monkeypatch):
    first = _course(id="remove-one", name="移除课程")
    second = _course(id="keep-one", name="保留课程")
    db = _prime_cart(monkeypatch, tmp_path, [first, second])
    enroll_service._reset_progress([first, second])

    assert enroll_service.reserve_enroll_task()
    waiter_result = []
    waiter = threading.Thread(
        target=lambda: waiter_result.append(enroll_service._wait_until_resumed())
    )
    try:
        assert enroll_service.pause_enroll_task()[0]
        waiter.start()
        deadline = time.monotonic() + 2
        while (
            not enroll_service.get_enroll_task_state()["pause_acknowledged"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert enroll_service.get_enroll_task_state()["pause_acknowledged"] is True

        removed = enroll_service.remove_cart_course(first.id)
        assert removed["success"] is True
        assert removed["task_stopping"] is False
        assert [row["id"] for row in db.get_courses_by_status("")] == [second.id]
        assert [row["id"] for row in enroll_service.get_enroll_progress()["courses"]] == [second.id]
        state = enroll_service.get_enroll_task_state()
        assert state["running"] is True
        assert state["paused"] is True
        assert state["pause_acknowledged"] is True

        assert enroll_service.resume_enroll_task()[0]
        waiter.join(timeout=2)
        assert waiter_result == [True]
    finally:
        enroll_service._release_enroll_task()
        enroll_service._set_progress_finished()
        waiter.join(timeout=2)


def test_terminal_course_can_be_removed_while_other_courses_are_running(tmp_path, monkeypatch):
    failed = _course(id="failed-remove", name="已失败课程")
    active = _course(id="active-keep", name="仍在抢课程")
    db = _prime_cart(monkeypatch, tmp_path, [failed, active])
    assert cart_service.update_status(failed.id, database.STATUS_FAILED)
    enroll_service._reset_progress([failed, active])
    enroll_service._update_course_progress(failed.id, status=database.STATUS_FAILED)

    assert enroll_service.reserve_enroll_task()
    try:
        removed = enroll_service.remove_cart_course(failed.id)

        assert removed["success"] is True
        assert removed["task_stopping"] is False
        assert [row["id"] for row in db.get_courses_by_status("")] == [active.id]
        progress_ids = [row["id"] for row in enroll_service.get_enroll_progress()["courses"]]
        assert progress_ids == [active.id]
        state = enroll_service.get_enroll_task_state()
        assert state["running"] is True
        assert state["paused"] is False
    finally:
        enroll_service._release_enroll_task()
        enroll_service._set_progress_finished()


def test_worker_launcher_uses_daemon_thread_and_reserves_once(monkeypatch):
    finished = threading.Event()
    observed = []

    def fake_worker(reserved=False):
        observed.append(reserved)
        enroll_service._release_enroll_task()
        finished.set()

    monkeypatch.setattr(enroll_service, "run_enroll_task", fake_worker)

    assert enroll_service.start_enroll_worker() is True
    assert finished.wait(timeout=2)
    assert observed == [True]
    assert enroll_service.is_enroll_task_running() is False


def test_worker_launcher_releases_reservation_when_thread_start_fails(monkeypatch):
    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(enroll_service.threading, "Thread", BrokenThread)

    with pytest.raises(RuntimeError, match="thread unavailable"):
        enroll_service.start_enroll_worker()
    assert enroll_service.is_enroll_task_running() is False


def test_raised_session_expiry_triggers_relogin(tmp_path, monkeypatch):
    course = _course(id="raised-expiry", name="异常过期课程")
    _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda *_args: (_ for _ in ()).throw(
            enroll_service.choose_course.SchoolSessionExpiredError("expired")
        ),
    )

    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.SESSION_EXPIRED


# ------------------------------------------------------------------
# 进度跟踪
# ------------------------------------------------------------------


def test_progress_snapshot_reports_success_and_event(tmp_path, monkeypatch):
    course = _course(id="P1", name="进度课")
    _prime_cart(monkeypatch, tmp_path, [course])
    monkeypatch.setattr(config, "count", 1)
    monkeypatch.setattr(
        enroll_service.choose_course,
        "submit_course_selection",
        lambda cid, ctype, campus: Resp("添加选课志愿成功"),
    )

    enroll_service._reset_progress([course])
    enroll_service.grab_courses([course])
    snapshot = enroll_service.get_enroll_progress()

    assert snapshot["counts"]["success"] == 1
    assert snapshot["counts"]["total"] == 1
    assert snapshot["courses"][0]["status"] == database.STATUS_SUCCESS
    assert any("已由学校已选课程列表确认选中" in event["message"] for event in snapshot["events"])


# ------------------------------------------------------------------
# 重登录续抢
# ------------------------------------------------------------------


def test_run_enroll_task_relogins_then_finishes(tmp_path, monkeypatch):
    db = DatabaseManager(str(tmp_path / "task.db"))
    monkeypatch.setattr(cart_service, "db", db)
    cart_service.add_course(_course(id="R1", name="续抢课"))  # PENDING

    grab_calls = []
    outcomes = [
        enroll_service.GrabOutcome.SESSION_EXPIRED,
        enroll_service.GrabOutcome.COMPLETED,
    ]

    def fake_grab(courses):
        grab_calls.append([c.id for c in courses])
        return outcomes[len(grab_calls) - 1]

    relogin_calls = []

    monkeypatch.setattr(enroll_service, "grab_courses", fake_grab)
    monkeypatch.setattr(
        enroll_service,
        "attempt_automatic_relogin",
        lambda max_attempts=config.ocr_relogin_max_attempts: relogin_calls.append(1) or (True, ""),
    )
    monkeypatch.setattr(config, "relogin_max_retries", 5)

    enroll_service.run_enroll_task(reserved=True)

    assert len(grab_calls) == 2
    assert len(relogin_calls) == 1
    assert grab_calls[0] == ["R1"]


def test_run_enroll_task_pauses_after_consecutive_relogin_failures(tmp_path, monkeypatch):
    db = DatabaseManager(str(tmp_path / "task2.db"))
    monkeypatch.setattr(cart_service, "db", db)
    cart_service.add_course(_course(id="R2", name="失败续抢课"))

    grab_outcomes = iter(
        [
            enroll_service.GrabOutcome.SESSION_EXPIRED,
            enroll_service.GrabOutcome.SESSION_EXPIRED,
            enroll_service.GrabOutcome.SESSION_EXPIRED,
            enroll_service.GrabOutcome.COMPLETED,
        ]
    )
    monkeypatch.setattr(enroll_service, "grab_courses", lambda courses: next(grab_outcomes))
    relogin_calls = []
    monkeypatch.setattr(
        enroll_service,
        "attempt_automatic_relogin",
        lambda max_attempts=config.ocr_relogin_max_attempts: (
            relogin_calls.append(1) or (False, "OCR 失败")
        ),
    )
    monkeypatch.setattr(enroll_service, "_wait_between_requests", lambda *_: True)
    monkeypatch.setattr(config, "relogin_max_retries", 3)

    worker = threading.Thread(target=enroll_service.run_enroll_task)
    worker.start()
    deadline = time.monotonic() + 2
    while not enroll_service.get_enroll_task_state()["paused"] and time.monotonic() < deadline:
        time.sleep(0.01)

    # 连续失败达到阈值后暂停并保留课程，不再写成永久失败。
    assert len(relogin_calls) == 3
    assert db.get_courses_by_status(database.STATUS_IN_PROGRESS)[0]["id"] == "R2"
    assert enroll_service.get_enroll_task_state()["pause_source"] == "relogin_failed"
    assert enroll_service.resume_enroll_task()[0]
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert db.get_courses_by_status(database.STATUS_FAILED) == []
