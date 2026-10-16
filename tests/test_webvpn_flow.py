"""Exercise the off-campus school flow with every HTTP request mocked."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

import choose_course
import config
import course_list
import logic
import study_program
from course_models import CoursesResponse
from services import auth_service, backend_service

WEBVPN_ORIGIN = "https://bkxk.webvpn.szu.edu.cn"
BASE_PATH = "/xsxkapp/sys/xsxkapp/"
GATEWAY_COOKIE = "_webvpn_key=key; webvpn_username=user; webvpn_username_NS_Sig=sig"
CAPTCHA_COOKIE = "route=fresh; insert_cookie=fresh"
SCHOOL_COOKIE = "JSESSIONID=fresh-session; _WEU=fresh-weu"
STUDENT_ID = "2024110122"
CLASS_ID = "202520262010192004801"
CAPTCHA_IMAGE = b"\xff\xd8\xff" + b"mock-image" * 3


@pytest.fixture(autouse=True)
def isolate_webvpn_state(monkeypatch, tmp_path):
    monkeypatch.setenv(study_program.PROGRAM_ENV, study_program.UNDERGRADUATE)
    for name, value in {
        "backend_preference": config.BACKEND_WEBVPN,
        "active_backend": config.BACKEND_PRIMARY,
        "webvpn_cookie": GATEWAY_COOKIE,
        "combined_cookie": "route=expired; JSESSIONID=stale; _WEU=stale",
        "token": "stale-token",
        "student_id": STUDENT_ID,
        "password": "test-password",
        "elective_batch_code": "old-batch",
        "elective_batch_name": "old-name",
        "campus_code": "01",
        "campus_name": "粤海校区",
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(auth_service, "_session_generation", 0)
    monkeypatch.setattr(logic, "_captcha_image_path", lambda: tmp_path / "captcha.jpg")
    backend_service.clear_primary_cooldown()
    yield
    backend_service.clear_primary_cooldown()


def _response(payload=None, *, headers=None, content=None, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode() if content is None else content
    response.headers.update(headers or {})
    return response


def _mock_http(monkeypatch, script):
    """Check the real request builders while replacing the HTTP transport."""
    calls = []

    def request(_session, method, url, **kwargs):
        index = len(calls)
        calls.append({"method": method.upper(), "url": url, **kwargs})
        assert index < len(script), f"Unexpected HTTP request: {method} {url}"
        expected_method, expected_path, response = script[index]
        assert method.upper() == expected_method
        assert urlsplit(url).path == BASE_PATH + expected_path
        assert urlsplit(url).netloc == "bkxk.webvpn.szu.edu.cn"
        assert urlsplit(url).scheme == "https"
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(requests.sessions.Session, "request", request)
    return calls


def _captcha_script():
    return [
        ("POST", "student/4/vcode.do", _response({"data": {"token": "fresh-vtoken"}})),
        (
            "GET",
            "student/vcode/image.do",
            _response(
                content=CAPTCHA_IMAGE,
                headers={"Set-Cookie": CAPTCHA_COOKIE, "Content-Type": "image/jpeg"},
            ),
        ),
    ]


@pytest.mark.parametrize("automatic_relogin", [False, True], ids=["manual", "automatic"])
def test_full_webvpn_flow_never_uses_the_campus_host(monkeypatch, automatic_relogin):
    script = [
        *_captcha_script(),
        (
            "POST",
            "student/check/login.do",
            _response(
                {"code": "1", "data": {"token": "fresh-token", "name": "Test Student"}},
                headers={"Set-Cookie": SCHOOL_COOKIE + "; _webvpn_key=rotated-key"},
            ),
        ),
        (
            "POST",
            f"student/{STUDENT_ID}.do",
            _response(
                {
                    "code": "1",
                    "data": {"electiveBatch": {"code": "2025202601", "typeName": "复选"}},
                }
            ),
        ),
        (
            "POST",
            "elective/programCourse.do",
            _response({"code": "1", "totalCount": 0, "dataList": []}),
        ),
        ("POST", "elective/volunteer.do", _response({"code": "1", "msg": "选课成功"})),
        (
            "POST",
            "elective/courseResult.do",
            _response({"code": "1", "dataList": [{"teachingClassID": CLASS_ID}]}),
        ),
    ]
    calls = _mock_http(monkeypatch, script)

    if automatic_relogin:
        monkeypatch.setattr(
            logic, "recognize_captcha_centers", lambda: [[1, 2], [3, 4], [5, 6], [7, 8]]
        )
        assert auth_service.attempt_automatic_relogin(max_attempts=1) == (True, "")
    else:
        captcha = logic.fetch_vtoken_and_image(max_attempts=1)
        captcha_cookie = logic.parse_cookie(captcha["cookie"])
        result = logic.login(
            STUDENT_ID, captcha["vtoken"], "encrypted", "1-2,3-4,5-6,7-8", captcha_cookie
        )
        assert result["success"] is True
        auth_service.save_login_state(
            result["cookie"], captcha_cookie, STUDENT_ID, "test-password", result["token"]
        )
        assert auth_service.refresh_elective_batch(STUDENT_ID, result["token"]) == "复选"

    assert config.token == "fresh-token"
    assert config.elective_batch_code == "2025202601"
    assert isinstance(course_list.programmed_course(0), CoursesResponse)
    assert choose_course.submit_course_selection(CLASS_ID, "FANKC").json()["code"] == "1"
    enrolled = choose_course.query_enrolled_courses(config.combined_cookie, config.token)
    assert choose_course.enrolled_teaching_class_ids(enrolled) == {CLASS_ID}
    assert len(calls) == len(script)

    rotated_gateway = GATEWAY_COOKIE.replace("_webvpn_key=key", "_webvpn_key=rotated-key")
    for index, call in enumerate(calls):
        headers = call["headers"]
        assert headers["Host"] == "bkxk.webvpn.szu.edu.cn"
        assert headers["Origin"] == WEBVPN_ORIGIN
        assert headers["Referer"] == WEBVPN_ORIGIN + BASE_PATH + "*default/index.do"
        assert "stale" not in headers["Cookie"]
        assert "expired" not in headers["Cookie"]
        if index < 2:
            assert headers["Cookie"] == GATEWAY_COOKIE
            assert "token" not in headers
        elif index == 2:
            assert headers["Cookie"] == f"{CAPTCHA_COOKIE}; {GATEWAY_COOKIE}"
            assert "token" not in headers
        else:
            assert headers["Cookie"] == f"{SCHOOL_COOKIE}; {CAPTCHA_COOKIE}; {rotated_gateway}"
            assert headers["token"] == "fresh-token"
    assert parse_qs(urlsplit(calls[1]["url"]).query) == {"vtoken": ["fresh-vtoken"]}
    assert calls[2]["data"]["vtoken"] == "fresh-vtoken"
    assert calls[2]["data"]["verifyCode"] == "1-2,3-4,5-6,7-8"
    catalog_query = json.loads(calls[4]["data"]["querySetting"])
    assert catalog_query["data"]["electiveBatchCode"] == "2025202601"
    assert CLASS_ID in calls[5]["data"]["addParam"]


def test_legacy_captcha_fetch_uses_webvpn_with_only_gateway_cookies(monkeypatch, tmp_path):
    calls = _mock_http(monkeypatch, _captcha_script())

    assert logic.get_new_image() == ("fresh-vtoken", CAPTCHA_COOKIE)

    assert (tmp_path / "captcha.jpg").read_bytes() == CAPTCHA_IMAGE
    assert len(calls) == 2
    assert all(call["headers"]["Cookie"] == GATEWAY_COOKIE for call in calls)


@pytest.mark.parametrize("operation", ["catalog", "enrollment"])
@pytest.mark.parametrize("failure", [requests.ConnectionError("unavailable"), 503])
def test_explicit_webvpn_failure_never_retries_on_primary(monkeypatch, operation, failure):
    response = _response(status=failure) if isinstance(failure, int) else failure
    endpoint = "elective/programCourse.do" if operation == "catalog" else "elective/volunteer.do"
    calls = _mock_http(monkeypatch, [("POST", endpoint, response)])

    def perform_request():
        if operation == "catalog":
            return course_list.programmed_course(0)
        return choose_course.submit_course_selection(CLASS_ID, "FANKC")

    if isinstance(failure, Exception):
        with pytest.raises(requests.ConnectionError):
            perform_request()
    else:
        assert perform_request().status_code == 503

    assert len(calls) == 1


def test_webvpn_enrollment_without_gateway_authentication_stops_before_http(monkeypatch):
    monkeypatch.setattr(config, "webvpn_cookie", "")
    calls = _mock_http(monkeypatch, [])

    with pytest.raises(backend_service.WebVPNAuthenticationRequiredError):
        choose_course.submit_course_selection(CLASS_ID, "FANKC")

    assert calls == []


def test_school_entry_and_catalog_headers_use_explicit_webvpn_selection():
    headers = course_list.get_headers()

    assert study_program.program_payload()["school_url"] == (
        WEBVPN_ORIGIN + BASE_PATH + "*default/index.do"
    )
    assert headers["Host"] == "bkxk.webvpn.szu.edu.cn"
    assert headers["Origin"] == WEBVPN_ORIGIN
    assert (
        headers["Referer"]
        == WEBVPN_ORIGIN + BASE_PATH + "*default/grablessons.do?token=stale-token"
    )
    assert headers["Cookie"] == f"{config.combined_cookie}; {GATEWAY_COOKIE}"


def test_scheduled_start_time_query_keeps_the_webvpn_session(monkeypatch):
    calls = _mock_http(
        monkeypatch,
        [
            (
                "POST",
                "elective/batch.do",
                _response(
                    {
                        "code": "1",
                        "dataList": [{"code": "old-batch", "beginTime": "2026-10-16 12:00:00"}],
                    }
                ),
            )
        ],
    )

    start = logic.fetch_undergraduate_start_time(
        config.elective_batch_code, config.token, config.combined_cookie
    )

    assert start == datetime(2026, 10, 16, 4, tzinfo=UTC)
    assert len(calls) == 1
    assert calls[0]["headers"]["Cookie"] == f"{config.combined_cookie}; {GATEWAY_COOKIE}"
    assert calls[0]["headers"]["token"] == config.token
    assert calls[0]["allow_redirects"] is False
