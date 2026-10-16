"""A second login page must not move existing school credentials to another host."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import requests
from starlette.testclient import TestClient

import app
import choose_course
import config
import logic
from services import auth_service
from study_program import PROGRAM_ENV, UNDERGRADUATE

GATEWAY_COOKIE = "_webvpn_key=test-key; webvpn_username=test-user; webvpn_username_NS_Sig=test-sig"
DIRECTIONS = [("webvpn", "primary"), ("primary", "webvpn")]
SESSION_FIELDS = (
    "backend_preference",
    "active_backend",
    "combined_cookie",
    "webvpn_cookie",
    "authserver_cookie",
    "token",
    "student_id",
    "password",
    "elective_batch_code",
    "elective_batch_name",
    "campus_code",
    "campus_name",
)


@pytest.fixture(autouse=True)
def isolate_session_binding(monkeypatch):
    monkeypatch.setenv(PROGRAM_ENV, UNDERGRADUATE)
    for field in SESSION_FIELDS:
        monkeypatch.setattr(config, field, "")
    monkeypatch.setattr(config, "backend_preference", config.BACKEND_PRIMARY)
    monkeypatch.setattr(config, "active_backend", config.BACKEND_PRIMARY)
    monkeypatch.setattr(config, "webvpn_cookie", GATEWAY_COOKIE)
    monkeypatch.setattr(auth_service, "_session_generation", 0)
    monkeypatch.setattr(auth_service, "_manual_login_count", 0)
    monkeypatch.setattr(auth_service, "_relogin_state", dict(auth_service._relogin_state))
    auth_service._reset_relogin_state_locked()
    monkeypatch.setattr(auth_service, "_automatic_relogin_worker", None)
    monkeypatch.setattr(app, "is_enroll_task_running", lambda: False)


def _bind_session(backend, *, expired=False):
    config.backend_preference = backend
    config.active_backend = backend
    config.student_id = "2024110122"
    config.password = "test-password"
    config.combined_cookie = "route=test-route; JSESSIONID=test-school-session"
    config.token = "test-school-token"
    config.elective_batch_code = "test-batch"
    config.elective_batch_name = "复选"
    if expired:
        auth_service.invalidate_school_session()


def _state():
    return {field: getattr(config, field) for field in SESSION_FIELDS}


def _login_payload(backend):
    return {
        "student_id": "2024110122",
        "password": "test-password",
        "card_key": "invalid-test-key",
        "vtoken": "test-vtoken",
        "verifyCode": [[1, 2], [3, 4], [5, 6], [7, 8]],
        "cookie": "route=test-captcha; insert_cookie=test-captcha",
        "backend": backend,
    }


def _call_api(endpoint, backend):
    client = TestClient(app.app)
    if endpoint == "captcha":
        return client.get("/api/captcha", params={"backend": backend})
    if endpoint == "login":
        return client.post("/api/login", json=_login_payload(backend))
    return client.post("/api/backend/select", json={"backend": backend})


def _assert_binding_conflict(response):
    assert response.status_code == 409
    assert response.json()["error_code"] == "BACKEND_SESSION_ACTIVE"
    assert response.json()["retryable"] is False


@pytest.mark.parametrize(("original", "requested"), DIRECTIONS)
@pytest.mark.parametrize("expired", [False, True], ids=["valid-session", "expired-credentials"])
@pytest.mark.parametrize("endpoint", ["select", "captcha", "login"])
def test_other_login_page_cannot_move_session_or_next_write(
    monkeypatch, original, requested, expired, endpoint
):
    _bind_session(original, expired=expired)
    before = _state()
    verify_key = Mock(return_value=False)
    monkeypatch.setattr(app, "verify_card_key", verify_key)
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"code":"1"}'
    transport = Mock(return_value=response)
    monkeypatch.setattr(requests.sessions.Session, "request", transport)

    _assert_binding_conflict(_call_api(endpoint, requested))

    assert _state() == before
    verify_key.assert_not_called()
    transport.assert_not_called()

    # Exercise the real enrollment URL/header/form builders after the rejected change.
    choose_course.submit_course_selection("test-class", "FANKC", "01")

    transport.assert_called_once()
    call = transport.call_args.kwargs
    expected_origin = (
        "https://bkxk.webvpn.szu.edu.cn" if original == "webvpn" else "http://bkxk.szu.edu.cn"
    )
    assert call["method"].upper() == "POST"
    assert call["url"] == expected_origin + "/xsxkapp/sys/xsxkapp/elective/volunteer.do"
    assert call["headers"]["Host"] == urlsplit(expected_origin).netloc
    assert call["headers"].get("token", "") == before["token"]
    sent_cookie = call["headers"].get("Cookie", "")
    if before["combined_cookie"]:
        assert before["combined_cookie"] in sent_cookie
    if original == "webvpn":
        assert GATEWAY_COOKIE in sent_cookie
    else:
        assert "_webvpn_key" not in sent_cookie
    assert config.backend_preference == original


@pytest.mark.parametrize(("original", "requested"), DIRECTIONS)
def test_fresh_login_page_can_select_either_backend(original, requested):
    config.backend_preference = original
    config.active_backend = original

    response = _call_api("select", requested)

    assert response.status_code == 200
    assert response.json()["preference"] == requested
    assert config.backend_preference == requested


@pytest.mark.parametrize("backend", ["primary", "webvpn"])
@pytest.mark.parametrize("endpoint", ["select", "captcha", "login"])
def test_existing_session_can_use_its_own_backend(monkeypatch, backend, endpoint):
    _bind_session(backend)
    before = _state()
    captcha = Mock(return_value={"vtoken": "test-vtoken", "imageUrl": "test-image"})
    verify_key = Mock(return_value=False)
    monkeypatch.setattr(logic, "fetch_vtoken_and_image", captcha)
    monkeypatch.setattr(app, "verify_card_key", verify_key)

    response = _call_api(endpoint, backend)

    assert response.status_code == (400 if endpoint == "login" else 200)
    assert response.json().get("error_code") != "BACKEND_SESSION_ACTIVE"
    assert _state() == before
    assert auth_service._manual_login_count == 0
    if endpoint == "captcha":
        captcha.assert_called_once_with(1)
    if endpoint == "login":
        verify_key.assert_called_once()


@pytest.mark.parametrize(("original", "requested"), DIRECTIONS)
def test_explicit_logout_releases_backend_binding(original, requested):
    _bind_session(original)

    auth_service.clear_login_state()
    config.webvpn_cookie = GATEWAY_COOKIE  # Synthetic gateway reauthentication after logout.
    response = _call_api("select", requested)

    assert response.status_code == 200
    assert config.backend_preference == requested
    assert not config.combined_cookie
    assert not config.token
    assert not config.student_id
    assert not config.password


@pytest.mark.parametrize(("original", "requested"), DIRECTIONS)
def test_manual_login_context_pins_backend_and_releases_after_exception(original, requested):
    with (
        pytest.raises(RuntimeError, match="synthetic login failure"),
        auth_service.manual_login_backend(original),
    ):
        assert auth_service.update_backend_preference(original) == original
        with pytest.raises(auth_service.BackendSelectionConflictError):
            auth_service.update_backend_preference(requested)
        assert config.backend_preference == original
        raise RuntimeError("synthetic login failure")

    assert auth_service._manual_login_count == 0
    assert auth_service.update_backend_preference(requested) == requested


@pytest.mark.parametrize(("original", "requested"), DIRECTIONS)
@pytest.mark.parametrize("endpoint", ["select", "captcha", "login"])
def test_second_request_cannot_switch_while_first_login_is_awaiting_school(
    monkeypatch, original, requested, endpoint
):
    entered_school_login = Event()
    finish_school_login = Event()
    monkeypatch.setattr(app, "verify_card_key", lambda *_: True)
    monkeypatch.setattr(app, "encrypt_password", lambda _: "test-encrypted-password")

    def waiting_login(*_args):
        entered_school_login.set()
        assert finish_school_login.wait(5), "Concurrent request did not finish"
        return {"success": False, "error_msg": "synthetic rejection"}

    monkeypatch.setattr(app, "perform_school_login", waiting_login)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_login = executor.submit(_call_api, "login", original)
        try:
            assert entered_school_login.wait(5), "First login did not reach the school stub"
            before = _state()
            _assert_binding_conflict(_call_api(endpoint, requested))
            assert _state() == before
            assert config.backend_preference == original
        finally:
            finish_school_login.set()
        assert first_login.result(timeout=5).status_code == 400

    assert auth_service._manual_login_count == 0
    assert _call_api("select", requested).status_code == 200


@pytest.mark.parametrize(("original", "requested"), DIRECTIONS)
@pytest.mark.parametrize(
    "operation", ["attempt_ocr_relogin", "attempt_automatic_relogin", "start_automatic_relogin"]
)
def test_automatic_relogin_cannot_replace_backend_or_retained_credentials(
    monkeypatch, original, requested, operation
):
    _bind_session(original, expired=True)
    before = _state()
    ocr = Mock(side_effect=AssertionError("A rejected backend change must not run OCR"))
    monkeypatch.setattr(logic, "verify_vcode_login_flow", ocr)
    params = {
        "backend": requested,
        "student_id": "2024110999",
        "password": "replacement-test-password",
    }

    if operation == "attempt_ocr_relogin":
        with pytest.raises(auth_service.BackendSelectionConflictError):
            auth_service.attempt_ocr_relogin(**params)
    else:
        success, message = getattr(auth_service, operation)(**params)
        assert success is False
        assert "访问入口" in message

    assert _state() == before
    assert auth_service._automatic_relogin_worker is None
    assert auth_service._relogin_state["status"] == "idle"
    ocr.assert_not_called()


@pytest.mark.parametrize(("original", "requested"), DIRECTIONS)
def test_running_recovery_pins_backend_even_if_credentials_have_been_cleared(original, requested):
    config.backend_preference = original
    auth_service._relogin_state["status"] = "running"

    with pytest.raises(auth_service.BackendSelectionConflictError):
        auth_service.update_backend_preference(requested)

    assert config.backend_preference == original


@pytest.mark.parametrize(("original", "requested"), DIRECTIONS)
def test_gateway_imported_school_cookie_does_not_bind_an_unfinished_login(original, requested):
    config.backend_preference = original
    config.combined_cookie = "route=test-gateway-route; JSESSIONID=test-imported-session"

    response = _call_api("select", requested)

    assert response.status_code == 200
    assert config.backend_preference == requested
    assert not config.token
    assert not config.student_id
    assert not config.password
