from __future__ import annotations

import json

import pytest

import choose_course
import config
import course_list
from course_models import CoursesResponse
from services import course_service


class DummyResponse:
    status_code = 200
    text = "mocked"


class DummyCourseResponse:
    status_code = 200
    text = '{"code":"1","dataList":[]}'

    def json(self):
        return {
            "totalCount": 0,
            "dataList": [],
            "msg": "查询推荐选课成功",
            "code": "1",
            "timestamp": "0",
        }


class LoginPageResponse:
    status_code = 200
    text = (
        '<form action="student/check/login.do"><input name="vtoken"><input name="loginPwd"></form>'
    )

    def json(self):
        raise ValueError("not json")

    def raise_for_status(self):
        return None


def test_enrollment_request_contract_is_source_compatible(monkeypatch):
    captured = {}

    def fake_post(**kwargs):
        captured.update(kwargs)
        return DummyResponse()

    monkeypatch.setattr(choose_course.requests, "post", fake_post)
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "elective_batch_code", "2025202601")
    monkeypatch.setattr(config, "combined_cookie", "route=x; JSESSIONID=y")
    monkeypatch.setattr(config, "token", "token")

    response = choose_course.submit_course_selection(
        "202520262010192004801",
        "FANKC",
    )

    assert response.status_code == 200
    assert captured["url"].endswith("elective/volunteer.do")
    assert set(captured["data"]) == {"addParam"}
    assert captured["data"]["addParam"] == (
        '{"data":{"operationType":"1","studentCode":2024110122,'
        '"electiveBatchCode":2025202601,"teachingClassId":202520262010192004801,'
        '"isMajor":"1","campus":"01","teachingClassType":FANKC,'
        '"chooseVolunteer":"1"}}'
    )
    assert captured["headers"]["Cookie"] == "route=x; JSESSIONID=y"
    assert captured["headers"]["token"] == "token"
    assert captured["timeout"] == choose_course.REQUEST_TIMEOUT


def test_recommended_course_uses_zero_based_school_page_and_dedicated_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return DummyCourseResponse()

    monkeypatch.setattr(course_list.requests, "post", fake_post)
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "elective_batch_code", "batch")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "token", "token")

    result = course_list.recommended_course(0)
    query = json.loads(captured["data"]["querySetting"])

    assert isinstance(result, CoursesResponse)
    assert captured["url"].endswith("elective/recommendedCourse.do")
    assert query["pageNumber"] == 0
    assert query["data"]["teachingClassType"] == "TJKC"
    assert captured["timeout"] == course_list.REQUEST_TIMEOUT


def test_course_service_rejects_negative_school_page():
    success, message, _ = course_service.query_courses("TJKC", -1)
    assert not success
    assert "大于等于 0" in message


def test_course_service_identifies_closed_course_window(monkeypatch):
    response = CoursesResponse(
        total_count=0,
        data_list=[],
        msg="当前非选课时间，课程目录未开放",
        code="0",
        timestamp="0",
    )
    monkeypatch.setitem(
        course_service.COURSE_TYPE_MAP,
        "TJKC",
        ("本班课程(推荐)", lambda _page: response),
    )

    success, error_code, _ = course_service.query_courses("TJKC", 0)

    assert success is False
    assert error_code == course_service.COURSE_WINDOW_CLOSED


@pytest.mark.parametrize(
    "message",
    (
        "请求过快，请稍后再试",
        "操作过于频繁",
        "Too Many Requests",
    ),
)
def test_course_service_identifies_only_explicit_throttling(monkeypatch, message):
    response = CoursesResponse(
        total_count=0,
        data_list=[],
        msg=message,
        code="0",
        timestamp="0",
    )
    monkeypatch.setattr(course_service, "pace_catalog_request", lambda: 0)
    monkeypatch.setitem(
        course_service.COURSE_TYPE_MAP,
        "TJKC",
        ("本班课程(推荐)", lambda _page: response),
    )

    success, error_code, _ = course_service.query_courses("TJKC", 0)

    assert success is False
    assert error_code == course_service.COURSE_QUERY_THROTTLED


def test_course_service_identifies_explicit_throttle_business_code(monkeypatch):
    response = CoursesResponse(
        total_count=0,
        data_list=[],
        msg="",
        code="429",
        timestamp="0",
    )
    monkeypatch.setattr(course_service, "pace_catalog_request", lambda: 0)
    monkeypatch.setitem(
        course_service.COURSE_TYPE_MAP,
        "TJKC",
        ("本班课程(推荐)", lambda _page: response),
    )

    success, error_code, _ = course_service.query_courses("TJKC", 0)

    assert success is False
    assert error_code == course_service.COURSE_QUERY_THROTTLED


def test_course_service_keeps_generic_school_rejection_out_of_throttle_retry(monkeypatch):
    response = CoursesResponse(
        total_count=0,
        data_list=[],
        msg="当前培养方案不允许查询该课程目录",
        code="0",
        timestamp="0",
    )
    monkeypatch.setattr(course_service, "pace_catalog_request", lambda: 0)
    monkeypatch.setitem(
        course_service.COURSE_TYPE_MAP,
        "TJKC",
        ("本班课程(推荐)", lambda _page: response),
    )

    success, error_code, _ = course_service.query_courses("TJKC", 0)

    assert success is False
    assert error_code == course_service.COURSE_QUERY_REJECTED


def test_phase_whitelist_matches_supported_enrollment_stages():
    assert config.is_automatic_enroll_phase("复选阶段")
    assert config.is_automatic_enroll_phase("正选")
    assert config.is_automatic_enroll_phase("补选阶段")
    assert config.is_automatic_enroll_phase("补退选")
    assert not config.is_automatic_enroll_phase("预选阶段")
    assert not config.is_automatic_enroll_phase("")


@pytest.mark.parametrize(
    "batch_name",
    (
        "补选尚未开放",
        "复选未开始",
        "正选暂停",
        "补选已结束",
        "补退选截止",
        "系统维护",
    ),
)
def test_closed_phase_keywords_override_automatic_keywords(batch_name):
    assert config.classify_elective_phase(batch_name) == config.PHASE_CLOSED
    assert not config.is_automatic_enroll_phase(batch_name)
    assert "未启动" in config.automatic_enroll_block_reason(batch_name)


def test_selected_course_login_page_is_not_treated_as_empty(monkeypatch):
    monkeypatch.setattr(
        choose_course.requests,
        "post",
        lambda **kwargs: LoginPageResponse(),
    )

    with pytest.raises(choose_course.SchoolSessionExpiredError):
        choose_course.query_enrolled_courses("cookie", "token")


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"code": "2", "msg": "系统繁忙", "dataList": []}, "rejected"),
        ({"code": "1", "msg": "查询成功"}, "no dataList"),
        ({"code": "1", "dataList": {}}, "must be a list"),
    ],
)
def test_selected_course_malformed_payload_is_not_treated_as_empty(monkeypatch, payload, error):
    class SelectedResponse:
        status_code = 200
        text = json.dumps(payload, ensure_ascii=False)

        def json(self):
            return payload

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        choose_course,
        "_school_request",
        lambda *_args, **_kwargs: SelectedResponse(),
    )

    with pytest.raises(ValueError, match=error):
        choose_course.query_enrolled_courses("cookie", "token")


def test_selected_course_id_validation_is_exact_and_strict():
    rows = [
        {"teachingClassID": " class-a "},
        {"teachingClassId": "class-b"},
        {"teaching_class_id": "class-c"},
    ]
    assert choose_course.enrolled_teaching_class_ids(rows) == {
        "class-a",
        "class-b",
        "class-c",
    }

    with pytest.raises(ValueError, match="has no teaching-class ID"):
        choose_course.enrolled_teaching_class_ids([{"courseName": "同名课程"}])
