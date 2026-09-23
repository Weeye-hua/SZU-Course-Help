from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

import choose_course
import config
import logic
from services import backend_service


@pytest.fixture(autouse=True)
def reset_backend_state(monkeypatch):
    backend_service.clear_primary_cooldown()
    monkeypatch.setattr(config, "backend_preference", config.BACKEND_AUTO)
    monkeypatch.setattr(config, "active_backend", config.BACKEND_PRIMARY)
    monkeypatch.setattr(config, "combined_cookie", "route=school")
    monkeypatch.setattr(config, "webvpn_cookie", "")
    yield
    backend_service.clear_primary_cooldown()


def _authenticate_webvpn(monkeypatch):
    monkeypatch.setattr(
        config,
        "webvpn_cookie",
        "_webvpn_key=key; webvpn_username=user; webvpn_username_NS_Sig=sig",
    )


def test_auto_mode_never_drops_the_only_usable_primary_backend(monkeypatch):
    backend_service.mark_primary_failure()

    profiles = backend_service.candidate_profiles(allow_failover=True)

    assert [profile.key for profile in profiles] == [config.BACKEND_PRIMARY]


def test_authenticated_read_only_request_can_fail_over(monkeypatch):
    _authenticate_webvpn(monkeypatch)
    calls = []

    def sender(**kwargs):
        calls.append(kwargs["url"])
        status_code = 503 if "bkxk.szu.edu.cn" in kwargs["url"] else 200
        return SimpleNamespace(status_code=status_code)

    response = backend_service.request_with_failover(
        "POST",
        "elective/programCourse.do",
        sender=sender,
        read_only=True,
    )

    assert response.status_code == 200
    assert calls[0].startswith("http://bkxk.szu.edu.cn/")
    assert calls[1].startswith("https://bkxk.webvpn.szu.edu.cn/")
    assert config.active_backend == config.BACKEND_WEBVPN


def test_mutating_request_never_crosses_backends_after_network_failure(monkeypatch):
    _authenticate_webvpn(monkeypatch)
    calls = []

    def sender(**kwargs):
        calls.append(kwargs["url"])
        raise requests.ConnectionError("primary unavailable")

    with pytest.raises(requests.ConnectionError):
        backend_service.request_with_failover(
            "POST",
            "elective/volunteer.do",
            sender=sender,
            read_only=False,
        )

    assert len(calls) == 1
    assert calls[0].startswith("http://bkxk.szu.edu.cn/")


def test_mutating_transient_response_is_returned_without_retry_or_false_success(monkeypatch):
    _authenticate_webvpn(monkeypatch)
    calls = []

    def sender(**kwargs):
        calls.append(kwargs["url"])
        return SimpleNamespace(status_code=503)

    response = backend_service.request_with_failover(
        "POST",
        "elective/deleteVolunteer.do",
        sender=sender,
        read_only=False,
    )

    assert response.status_code == 503
    assert len(calls) == 1
    assert backend_service.primary_cooldown_active() is True


def test_explicit_webvpn_requires_authenticated_gateway_cookie():
    with pytest.raises(backend_service.WebVPNAuthenticationRequiredError):
        backend_service.request_with_failover(
            "GET",
            "elective/status.do",
            sender=lambda **kwargs: SimpleNamespace(status_code=200),
            preference=config.BACKEND_WEBVPN,
            read_only=True,
        )


def test_enrollment_stays_on_primary_after_read_only_webvpn_fallback(monkeypatch):
    _authenticate_webvpn(monkeypatch)
    monkeypatch.setattr(config, "active_backend", config.BACKEND_WEBVPN)
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "elective_batch_code", "batch")
    monkeypatch.setattr(config, "token", "token")
    calls = []

    def sender(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(choose_course.requests, "post", sender)

    choose_course.submit_course_selection("class-id", "FANKC", "01")

    assert len(calls) == 1
    assert calls[0]["url"].startswith("http://bkxk.szu.edu.cn/")
    assert "_webvpn_key" not in calls[0]["headers"]["Cookie"]


def test_school_login_stays_on_primary_and_excludes_webvpn_cookie(monkeypatch):
    _authenticate_webvpn(monkeypatch)
    monkeypatch.setattr(config, "active_backend", config.BACKEND_WEBVPN)
    monkeypatch.setattr(config, "backend_preference", config.BACKEND_WEBVPN)
    captured = {}

    class Response:
        headers = {}

        @staticmethod
        def json():
            return {"code": "0", "msg": "preview failure"}

    def request_with_failover(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(backend_service, "request_with_failover", request_with_failover)

    result = logic.login("2024110122", "vtoken", "encrypted", "1-2,3-4,5-6,7-8", "route=captcha")

    assert result["success"] is False
    assert captured["preference"] == config.BACKEND_PRIMARY
    assert "_webvpn_key" not in captured["cookie"]


def test_captcha_token_request_follows_selected_backend(monkeypatch):
    _authenticate_webvpn(monkeypatch)
    monkeypatch.setattr(config, "backend_preference", config.BACKEND_WEBVPN)
    captured = {}

    class Response:
        text = '{"data":{"token":"vtoken"}}'

        @staticmethod
        def json():
            return {"data": {"token": "vtoken"}}

        @staticmethod
        def raise_for_status():
            return None

    def request_with_failover(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(backend_service, "request_with_failover", request_with_failover)

    assert logic.get_vtoken() == "vtoken"
    assert captured["read_only"] is True
    assert captured["preference"] == config.BACKEND_WEBVPN
    assert captured["omit_cookie"] is True


def test_captcha_token_and_image_use_webvpn_urls(monkeypatch):
    _authenticate_webvpn(monkeypatch)
    monkeypatch.setattr(config, "backend_preference", config.BACKEND_WEBVPN)
    calls = []

    class TokenResponse:
        status_code = 200
        text = '{"data":{"token":"vtoken"}}'

        @staticmethod
        def json():
            return {"data": {"token": "vtoken"}}

        @staticmethod
        def raise_for_status():
            return None

    class ImageResponse:
        status_code = 200
        content = b"\xff\xd8\xff\x00" + b"0" * 16
        headers = {
            "Set-Cookie": "route=fresh; insert_cookie=fresh; Path=/",
            "Content-Type": "image/jpeg",
        }

        @staticmethod
        def raise_for_status():
            return None

    def fake_post(**kwargs):
        calls.append(kwargs)
        return TokenResponse()

    def fake_get(**kwargs):
        calls.append(kwargs)
        return ImageResponse()

    monkeypatch.setattr(logic.requests, "post", fake_post)
    monkeypatch.setattr(logic.requests, "get", fake_get)

    result = logic._fetch_vtoken_and_image_once()

    assert result["vtoken"] == "vtoken"
    assert len(calls) == 2
    assert calls[0]["url"].startswith(
        "https://bkxk.webvpn.szu.edu.cn/xsxkapp/sys/xsxkapp/student/4/vcode.do?timestamp="
    )
    assert calls[1]["url"] == (
        "https://bkxk.webvpn.szu.edu.cn/xsxkapp/sys/xsxkapp/student/vcode/image.do?vtoken=vtoken"
    )
    assert all(call["headers"]["Host"] == "bkxk.webvpn.szu.edu.cn" for call in calls)
    assert all(call["headers"]["Origin"] == "https://bkxk.webvpn.szu.edu.cn" for call in calls)
    assert all(
        call["headers"]["Referer"].startswith("https://bkxk.webvpn.szu.edu.cn/") for call in calls
    )
    assert all(
        call["headers"]["Cookie"]
        == "_webvpn_key=key; webvpn_username=user; webvpn_username_NS_Sig=sig"
        for call in calls
    )


@pytest.mark.parametrize("existing_cookie", ["route=expired; JSESSIONID=stale", ""])
def test_omit_cookie_removes_header_instead_of_sending_empty_value(
    monkeypatch,
    existing_cookie,
):
    monkeypatch.setattr(config, "combined_cookie", existing_cookie)
    captured = {}

    def sender(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status_code=200)

    backend_service.request_with_failover(
        "GET",
        "student/vcode/image.do?vtoken=test",
        sender=sender,
        preference=config.BACKEND_PRIMARY,
        read_only=True,
        omit_cookie=True,
    )

    assert "Cookie" not in captured["headers"]


def test_omit_cookie_keeps_webvpn_gateway_cookie(monkeypatch):
    _authenticate_webvpn(monkeypatch)
    captured = {}

    def sender(**kwargs):
        captured.update(kwargs["headers"])
        return SimpleNamespace(status_code=200)

    backend_service.request_with_failover(
        "GET",
        "student/vcode/image.do?vtoken=test",
        sender=sender,
        omit_cookie=True,
        read_only=True,
        preference=config.BACKEND_WEBVPN,
    )

    assert captured["Cookie"] == "_webvpn_key=key; webvpn_username=user; webvpn_username_NS_Sig=sig"


def test_inherit_explicit_and_empty_policies_keep_existing_cookie_behaviour():
    captured = {}

    def sender(**kwargs):
        captured.update(kwargs["headers"])
        return SimpleNamespace(status_code=200)

    backend_service.request_with_failover(
        "GET", "elective/status.do", sender=sender, read_only=True
    )
    assert captured["Cookie"] == "route=school"

    backend_service.request_with_failover(
        "GET",
        "elective/status.do",
        sender=sender,
        read_only=True,
        cookie="route=explicit",
    )
    assert captured["Cookie"] == "route=explicit"

    backend_service.request_with_failover(
        "GET", "elective/status.do", sender=sender, read_only=True, cookie=""
    )
    assert captured["Cookie"] == ""


def test_get_new_image_requests_cookie_omission(monkeypatch):
    captured = {}

    class ImageResponse:
        status_code = 200
        content = b"\xff\xd8\xff\x00" + b"0" * 16
        headers = {
            "Set-Cookie": "route=fresh; insert_cookie=fresh; Path=/",
            "Content-Type": "image/jpeg",
        }

        def raise_for_status(self):
            return None

    def request_with_failover(*_args, **kwargs):
        captured.update(kwargs)
        return ImageResponse()

    monkeypatch.setattr(config, "combined_cookie", "route=expired; JSESSIONID=stale")
    monkeypatch.setattr(logic, "get_vtoken", lambda **_kwargs: "vtoken")
    monkeypatch.setattr(backend_service, "request_with_failover", request_with_failover)

    _vtoken, cookie = logic.get_new_image()

    assert "route=fresh" in cookie
    assert captured["omit_cookie"] is True


def test_school_login_submits_only_this_rounds_captcha_cookie(monkeypatch):
    monkeypatch.setattr(config, "combined_cookie", "route=expired; JSESSIONID=stale")
    captured = {}

    class Response:
        headers = {}

        @staticmethod
        def json():
            return {"code": "0", "msg": "rejected"}

    def request_with_failover(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(backend_service, "request_with_failover", request_with_failover)

    result = logic.login(
        "2024110122",
        "vtoken",
        "encrypted",
        "1-2,3-4,5-6,7-8",
        "route=fresh; insert_cookie=fresh",
    )

    assert result["success"] is False
    assert captured["cookie"] == "route=fresh; insert_cookie=fresh"
    assert captured["omit_cookie"] is False


def test_school_login_request_header_carries_only_captcha_cookie(monkeypatch):
    monkeypatch.setattr(config, "combined_cookie", "route=expired; JSESSIONID=stale")
    captured = {}

    class Response:
        headers = {}

        @staticmethod
        def json():
            return {"code": "0", "msg": "rejected"}

    def post(**kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(requests, "post", post)

    logic.login("2024110122", "vtoken", "encrypted", "1-2,3-4,5-6,7-8", "route=fresh")

    assert captured["headers"]["Cookie"] == "route=fresh"


def test_school_login_without_captcha_cookie_omits_the_header(monkeypatch):
    captured = {}

    class Response:
        headers = {}

        @staticmethod
        def json():
            return {"code": "0", "msg": "rejected"}

    def post(**kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(requests, "post", post)

    logic.login("2024110122", "vtoken", "encrypted", "1-2,3-4,5-6,7-8", "")

    assert "Cookie" not in captured["headers"]
